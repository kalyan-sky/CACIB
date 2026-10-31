# EKS through a jump host, on Fargate

Banks don't let engineers reach production clusters from their laptops. You connect to a
**jump host** (bastion), and only the jump host can reach the Kubernetes API. This guide sets
that up with a GCP VM as the jump host, and runs the pods on **AWS Fargate** instead of EC2
worker nodes.

```
 laptop ──SSH──► GCP VM (jump host, static IP)
                  aws CLI · kubectl · terraform · this repo · terraform.tfstate
                    │ HTTPS, allowed only from the jump host /32
                    ▼
                 EKS API endpoint ── Fargate profiles ──► one micro-VM per pod
                                                          (private subnets, images via NAT)
```

Cost: about **USD 0.25–0.30 per hour**. That is EKS $0.10, NAT gateway ~$0.06, and roughly
$0.015 per running pod. Destroy everything when you finish.

## Step 0: is anything still running from CloudShell?

The old Terraform state lives in **CloudShell** (`~/CACIB/terraform/terraform.tfstate`).
From the jump host, run:
```bash
aws eks list-clusters --region ap-southeast-2
```
If `bank-sim` is listed, destroy it **from CloudShell** (`cd ~/CACIB && YES=1 make tf-down`)
before going on. From now on, the jump host holds the state.

## Step 1: Prepare the jump host (once)

1. **Make its IP static.** In GCP go to **VPC network → IP addresses**, find the VM's external
   IP and choose **Promote to static IP address**. Otherwise the IP changes when the VM
   restarts and you lock yourself out of the cluster.
2. **Lock down SSH.** In **VPC network → Firewall**, allow port 22 only from your own IP
   (`112.199.221.112/32`), or use IAP (`35.235.240.0/20`) with `gcloud compute ssh --tunnel-through-iap`.
3. On the VM:
```bash
git clone -b claude/nifty-noether-tbarsc https://github.com/kalyan-sky/CACIB.git ~/CACIB
cd ~/CACIB
./scripts/jumphost-setup.sh      # git, make, python3, kubectl 1.37, terraform; prints your AWS identity and IP
source ~/.bashrc
```

## Step 2: Settings

```bash
cd ~/CACIB/terraform
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars
```
Set these values; leave the rest as they are:
```hcl
region           = "ap-southeast-2"
compute          = "fargate"
api_public_cidrs = ["<the IP jumphost-setup.sh printed>/32"]
# optional: your console identity, so EKS → Resources works in the console.
# In CloudShell run `aws sts get-caller-identity`; skip it if the ARN ends in :root
# admin_principal_arns = ["arn:aws:iam::<account>:user/<name>"]
```

**Check your Fargate quota.** New accounts can have a low limit:
```bash
aws service-quotas get-service-quota --service-code fargate --quota-code L-3032A538 \
  --region ap-southeast-2 --query Quota.Value
```
The classics need about 2 vCPU, so anything **≥ 4** is enough. If the value is lower,
request an increase in **Service Quotas → AWS Fargate**.

## Step 3: Build it

```bash
terraform init
terraform plan -out tfplan       # read it: no node group or launch template; 2 Fargate profiles, a NAT gateway, a Fargate role
terraform apply tfplan           # ~15-20 min
$(terraform output -raw configure_kubectl)
kubectl get pods -n kube-system -o wide    # CoreDNS + metrics-server, each on its own fargate-ip-… node
```

Look at it in the console too:
- **EKS → bank-sim → Compute** shows **Fargate profiles** `system` and `workloads` and no node groups.
- **EKS → Networking** shows the endpoint as *Public and private*, with your /32 under **Public access source allowlist**.
- **VPC → NAT gateways** shows the NAT gateway that lets the private subnets pull images.

**Prove the bastion works.** In **CloudShell** run `aws eks update-kubeconfig --name bank-sim --region ap-southeast-2 && kubectl get ns`.
It **times out**: CloudShell's IP isn't allowed. The same command on the jump host works. The
AWS API (`aws eks describe-cluster`) still works from both, because the allow-list protects
only the Kubernetes API.

## Step 4: The classics on Fargate

```bash
cd ~/CACIB
./k8s-classics.sh list
./k8s-classics.sh start c04
```

| Case | On Fargate |
|---|---|
| c01–c03, c07–c13, c15 | Same symptoms. Pods take **~1 minute** to start, because each one gets a new micro-VM |
| c04 Insufficient cpu | Still Pending, but expect the event from **`fargate-scheduler`**, not `default-scheduler`. Fargate's largest pod size is 16 vCPU, so a 64-CPU request can never fit. Read the message: it's a different wording for the same root cause |
| c05, c06, c14 | **Skipped automatically.** There are no nodes to label or taint, and no EBS on Fargate (only EFS) |
| c16 CoreDNS down | Works. CoreDNS itself runs on Fargate, so recovery takes a minute longer |

New things to look at:
```bash
kubectl get nodes                               # one "node" per running pod
kubectl -n <ns> get pod <pod> -o yaml | grep -A3 annotations   # CapacityProvisioned: the Fargate size you pay for
kubectl -n <ns> describe pod <pod> | grep -i fargate
```
Use `kubectl top pods -A` for usage. Fargate rounds every pod up to a fixed size (0.25 vCPU / 0.5 GB minimum).

## What Fargate can't do (interview answers)

| Feature | Why not on Fargate | Use instead |
|---|---|---|
| DaemonSets (log agents, node exporters) | There are no nodes | A sidecar, or Fargate's built-in Fluent Bit logging (`aws-observability` ConfigMap) |
| EBS volumes | EBS attaches to EC2 instances | EFS (`efs.csi.aws.com`) |
| NetworkPolicies | VPC CNI policy enforcement needs nodes | Security groups for pods; the platform's NetworkPolicies are **not enforced** here |
| `type: LoadBalancer` Services | The built-in controller targets instances | AWS Load Balancer Controller with IP targets. It needs IRSA or Pod Identity; IRSA is blocked by this account's SCP |
| Privileged pods, hostNetwork, hostPath | Every pod is an isolated VM | Nothing, and that's a security selling point |

So for the **bank simulation** (NetworkPolicy scenario 03, load balancers, Argo CD UI) keep
`compute = "ec2"`. To reach the Argo CD UI through the jump host, run
`kubectl -n argocd port-forward svc/argocd-server 8080:443` on the VM, and from your laptop
`gcloud compute ssh <vm> -- -L 8080:localhost:8080`, then open https://localhost:8080.

## End of the session (on the jump host)

```bash
cd ~/CACIB
./k8s-classics.sh reset-all
YES=1 make tf-down               # ~10-15 min; removes the Fargate profiles, NAT gateway, cluster and VPC
aws eks list-clusters --region ap-southeast-2    # []
```
Stop the GCP VM too if you don't need it (**Compute Engine → VM instances → Stop**). Keep the
static IP: an unused static IP costs a little, but it keeps your tfvars valid.

To switch back to nodes later, set `compute = "ec2"` and `terraform apply`.

## Talking points

- **Bastion patterns:** the gold standard in AWS is a **private-only** endpoint plus a bastion
  *inside* the VPC, reached with **SSM Session Manager**: no SSH port, no public IP, and every
  session logged. Here the jump host is in another cloud, so the endpoint stays public but
  allows only one /32. Explain both and why a bank prefers the first.
- **Credentials on the jump host:** `aws configure` stores long-lived access keys on a VM in
  another cloud, which is the weak spot of this setup. Better options are **IAM Identity
  Center** (`aws sso login --use-device-code`, short-lived credentials) or **workload
  identity federation** (the GCP VM's service account token exchanged with
  `AssumeRoleWithWebIdentity`, with no stored keys at all). Rotate the access keys and give
  them least privilege.
- **Terraform state** now lives only on the jump host. If the VM dies, the state is gone. A
  team uses a remote backend (S3 with `use_lockfile`), so any approved jump host can run it.
- **Fargate vs EC2:** Fargate means no node patching (AWS does it), per-pod isolation and
  per-pod billing. You give up DaemonSets, EBS, privileged workloads and NetworkPolicy, and
  each pod takes about a minute to start. Many banks mix both: Fargate for batch and
  untrusted workloads, node groups for the platform.
