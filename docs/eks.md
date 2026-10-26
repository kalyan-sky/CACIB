# Running the simulation on Amazon EKS

The same bank, the same 216 apps and the same 10 tickets, on a real managed
Kubernetes cluster in **AWS Singapore (`ap-southeast-1`)**. It also builds
AWS skills (EKS, ECR, VPC CNI, managed node groups, IAM) that many banks use
next to, or instead of, their private cloud.

> **This costs money.** EKS is not in the AWS free tier. Run it for a study
> session, then delete it with `make eks-down`. Don't leave it running overnight.

## What changes compared with kind

| | kind (laptop) | EKS |
|---|---|---|
| Control plane | container on your laptop | managed by AWS (you can't see or patch it) |
| Zones | fake labels `sg-dc1-a`... | real AZs `ap-southeast-1a/b/c`, one node in each |
| Image | `kind load` into the nodes | built for `linux/amd64` and pushed to **Amazon ECR** |
| Networking / NetworkPolicy | kindnet | **AWS VPC CNI** with network policy enabled; pods get VPC IP addresses |
| Pod density | unlimited | ~17 pods per `t3.medium` by default, so **prefix delegation** is enabled to allow 110 |
| Node patching (scenario 07) | `kubectl drain` | the same, plus real **managed node group** AMI upgrades |

Everything else stays the same: rendered manifests, quotas, Pod Security, RBAC, `sim.sh`.
The audit accepts ECR as an approved registry.

## 1. Prerequisites

- An AWS account in which you can create EKS, EC2, VPC, IAM roles, CloudFormation stacks and ECR.
  Admin rights on a personal or sandbox account is simplest. Brand-new accounts sometimes have a
  low vCPU limit (3 x t3.medium needs 6 vCPU).
- Tools:
  ```bash
  # macOS (Homebrew); on Linux see each project's install page
  brew install awscli eksctl kubectl
  pip install -r requirements.txt
  # Docker Desktop (includes buildx)
  ```
- Region: CloudShell sets `AWS_REGION` to the console's region, and `eks-up` uses it. Choose Singapore
  in the console's region menu, or run `AWS_REGION=ap-southeast-1 make eks-up`.
- Credentials:
  ```bash
  aws configure            # or: aws configure sso && aws sso login
  aws sts get-caller-identity
  ```

### Using AWS CloudShell instead of your laptop

[CloudShell](https://console.aws.amazon.com/cloudshell) (the `>_` icon in the AWS console) is already
logged in to your account and needs no `aws configure`. It runs Amazon Linux, so use `dnf`, not `apt`.
It has **no Docker**: `eks-up.sh` notices this, compiles the image with Go and pushes it to ECR with
[crane](https://github.com/google/go-containerregistry/tree/main/cmd/crane).

```bash
# tools go into ~/bin because only your home directory survives between CloudShell sessions
mkdir -p ~/bin && grep -q 'HOME/bin' ~/.bashrc || echo 'export PATH=$HOME/bin:$PATH' >> ~/.bashrc
export PATH=$HOME/bin:$PATH
curl -sSL "https://github.com/eksctl-io/eksctl/releases/latest/download/eksctl_Linux_amd64.tar.gz" | tar xz -C ~/bin
curl -sSL -o ~/bin/kubectl "https://dl.k8s.io/release/$(curl -sSL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
chmod +x ~/bin/kubectl
sudo dnf install -y golang make git        # Go builds the image; reinstall after a new session
pip3 install --user pyyaml
```

CloudShell ends a session after ~20-30 minutes **without keyboard input**, and cluster creation takes
about that long. Press a key now and then while `make eks-up` runs. If the session drops anyway,
open a new one, re-run the setup lines above, and run `make eks-up` again. It reuses the cluster and
creates whatever is missing (for example the node group).

## 2. Cost (rough, on-demand, ap-southeast-1)

| Item | Approx. per hour |
|---|---|
| EKS control plane | $0.10 |
| 3 x t3.medium nodes | ~$0.16 |
| 3 x 30 GB gp3 volumes, 3 public IPv4 addresses | ~$0.03 |
| **Total, lite profile** | **~$0.30 / hour (~$7 / day)** |
| `INSTANCE_TYPE=t3.large` (for `PROFILE=full`) | +~$0.16 / hour |
| `PRIVATE_NODES=1` (NAT gateway) | +~$0.06 / hour + data |

These are estimates. Check the [AWS Pricing Calculator](https://calculator.aws/) for current prices,
and set a budget alert in **Billing → Budgets** before you start.

## 3. Create the cluster and deploy the bank

```bash
make eks-up                                   # lite profile, 3 x t3.medium in ap-southeast-1
# options:
PROFILE=full INSTANCE_TYPE=t3.large make eks-up   # all 216 apps
PRIVATE_NODES=1 make eks-up                       # bank-like: nodes in private subnets behind NAT
AWS_REGION=ap-southeast-3 make eks-up             # another region (needs a, b and c AZs)
```

`scripts/eks-up.sh` does this, in about 20 minutes:

1. Checks your tools and AWS identity.
2. Runs `eksctl create cluster` from [`platform/eks-cluster.yaml`](../platform/eks-cluster.yaml):
   a VPC across 3 AZs, an OIDC provider, the VPC CNI addon with `enableNetworkPolicy` and
   prefix delegation, and a managed node group of 3 nodes (`maxPodsPerNode: 110`).
3. Adds the kubectl context `eks-bank-sim` and checks pod capacity and the network-policy agent.
4. Creates the ECR repository `bank-sim/txn-service` (scan on push). It builds the image for `linux/amd64`
   (which works from Apple Silicon too) and pushes `:1.4.0` and `:latest`.
5. Runs `scripts/deploy.sh`: cluster RBAC, render with the ECR image, apply, wait for every deployment.

The context and image are saved in `.sim-state/`. After that, `./sim.sh`, `make audit`,
`make render`, `make apply` etc. all target EKS automatically.

## 4. Use it

```bash
./sim.sh status
./sim.sh dashboard            # port-forward, http://localhost:8080
./sim.sh page                 # random incident
make audit                    # live control audit
kubectl get nodes -L topology.kubernetes.io/zone,node.kubernetes.io/instance-type
```

Extra EKS drills:

- **Look at it from AWS:** EKS console → cluster `bank-sim` → Resources / Compute. Also EC2 → network
  interfaces (pod IPs are real VPC IPs).
- **Network policy agent logs** while you do scenario 03:
  `kubectl -n kube-system logs ds/aws-node -c aws-network-policy-agent --tail=50`
- **Scenario 07 for real:** after fixing the PDB, run
  `eksctl upgrade nodegroup --cluster bank-sim --name workers --region ap-southeast-1`.
  With the broken PDB the upgrade fails with `PodEvictionFailure`. That is the same lesson at cloud scale.
- **ECR:** `aws ecr describe-image-scan-findings --repository-name bank-sim/txn-service --image-id imageTag=1.4.0 --region ap-southeast-1`
- **Who am I in the cluster?** `kubectl auth whoami` and `aws eks list-access-entries --cluster-name bank-sim --region ap-southeast-1`
  (how AWS IAM maps to Kubernetes RBAC).

## 5. Delete everything

```bash
make eks-down          # asks you to type the cluster name; YES=1 skips the prompt
```

This deletes the cluster (nodes, volumes, VPC, CloudFormation stacks) and the ECR repository.
To double-check:
```bash
eksctl get cluster --region ap-southeast-1
aws cloudformation list-stacks --region ap-southeast-1 --stack-status-filter CREATE_COMPLETE | grep eksctl-bank-sim
aws ecr describe-repositories --region ap-southeast-1
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Pods `Pending`, event `Too many pods` | Prefix delegation did not reach the nodes. `eks-up.sh` warns if allocatable pods < 100. Recreate the node group: `eksctl delete nodegroup` then `eksctl create nodegroup -f <config>`. |
| `ImagePullBackOff` from ECR with `exec format error` in logs | The image was built for arm64. Rebuild with `--platform linux/amd64` (eks-up.sh does this). |
| `ImagePullBackOff`, 403 | The node role lacks `AmazonEC2ContainerRegistryReadOnly` (eksctl adds it by default), or the region doesn't match. |
| Scenario 03 doesn't break | Network policy isn't enforced. `kubectl -n kube-system get ds aws-node -o yaml | grep -i network-policy`, and check the vpc-cni addon config in the EKS console. |
| `error: You must be logged in to the server (Unauthorized)` | Stale credentials. Run `aws sso login` / `aws sts get-caller-identity`, then `aws eks update-kubeconfig --name bank-sim --region ap-southeast-1 --alias eks-bank-sim`. |
| Auto Scaling activity says `The specified instance type is not eligible for Free Tier` | The account is on the AWS **Free plan**, which only launches free-tier-eligible types. List them with `aws ec2 describe-instance-types --filters Name=free-tier-eligible,Values=true --query 'InstanceTypes[].InstanceType'`, then use one, e.g. `INSTANCE_TYPE=m7i-flex.large` (2 vCPU / 8 GiB). Usage is paid from the plan's credits. |
| Node group stuck in `CREATING` with no Auto Scaling group; eksctl ends with `exceeded max wait time` | EKS can't create the nodes on your behalf. This is common in sandbox accounts. Reuse the cluster with a self-managed node group: `NODEGROUP_TYPE=self make eks-up`. It adds `workers-self` next to the stuck `workers`. Everything in the lab works the same, except that patching is "create a new node group, delete the old one" instead of `eksctl upgrade nodegroup`. |
| `AccessDenied ... with an explicit deny in a service control policy` | Your AWS Organization blocks that API, and only its admin can change that. The cluster config no longer needs OIDC, the usual blocked call. If a run failed half-way, `make eks-down` deletes the leftovers through CloudFormation when eksctl is blocked. Other denials (instance types, regions) need a different `INSTANCE_TYPE` / `AWS_REGION`, or a request to your admin. |
| `eksctl create cluster` fails on capacity / vCPU limit | Request a quota increase (Service Quotas → EC2 → Running On-Demand Standard instances), or try another instance type. |
| Switching back to kind | `make up` re-targets kind. To point at EKS again: `echo eks-bank-sim > .sim-state/context` (and the ECR image in `.sim-state/image`), or re-run `make eks-up`. It reuses the existing cluster. |

## Talking points this gives you

- EKS shared responsibility: AWS runs the control plane. You still own nodes, AMIs, add-on versions, upgrades and workloads.
- VPC CNI: pods take VPC IPs, so you plan subnets and IPs. Prefix delegation vs custom networking.
- IAM ↔ RBAC: access entries / aws-auth, IRSA / Pod Identity for workloads that call AWS APIs.
- ECR: scan on push, immutable tags, lifecycle policies, pull-through cache. These are the cloud equivalents
  of the bank's internal Harbor/Artifactory.
- Hybrid: many banks run sensitive workloads on a private cloud and use EKS (or EKS Anywhere /
  EKS on Outposts) for others. The same golden path and guardrails apply to both.
