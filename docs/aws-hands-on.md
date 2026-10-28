# Hands-on AWS: debugging on EKS with the console and the CLI

The classic cases (`./k8s-classics.sh`) run on any cluster. On **EKS** you can also practise
the AWS side of each problem: where it shows in the console, which AWS service is involved, and
the failures that only exist on AWS. Interviewers for cloud roles ask both halves:
"the pod is Pending; and what would you check in AWS?"

## Set up a practice session (~25 min, ~USD 0.45/hour from your credits)

```bash
export PATH=$HOME/bin:$PATH AWS_REGION=ap-southeast-2 TF_DATA_DIR=/tmp/terraform-data
cd ~/CACIB && git pull gitlab main
cd terraform && terraform init && terraform apply           # 32 resources, ~20 min
aws eks update-kubeconfig --name bank-sim --region ap-southeast-2 --alias eks-bank-sim
cd .. && ./k8s-classics.sh list
```
You don't need Argo CD or the bank for the classics. When you finish: `YES=1 make tf-down`.
It deletes PVC volumes and load balancers first, then everything Terraform built.

## Part 1: the classics, plus where AWS shows them

Do each case as usual (`start` → investigate → `check` → `solution`), then look at the AWS view too.

| Case | AWS console view worth knowing |
|---|---|
| c01–c03, c08–c10, c13, c15 | **EKS → bank-sim → Resources → Workloads → Pods**: status, events and YAML without kubectl. The console shows **no container logs**; those need `kubectl logs` or CloudWatch Container Insights |
| c02 ImagePullBackOff | With a private registry: **ECR → Repositories** (does the tag exist?), and the node role has `AmazonEC2ContainerRegistryReadOnly` (**IAM → Roles → bank-sim-node**) |
| c04 Insufficient cpu | **EKS → Compute → workers**: instance type and node count. Real fixes on AWS are a bigger instance type or more nodes (the Free plan limits which types). **EC2 → Instances**: vCPU/memory per node |
| c05/c06 node selector/taint | **EKS → Compute → Nodes → node → Labels / Taints**. Managed node groups can set labels and taints in the node group config (**Edit node group**) |
| c07 OOMKilled | **EC2 → Instances → Monitoring**: node memory is fine, so the *container limit* killed it, not the node |
| c11/c16 DNS | **EKS → Add-ons → CoreDNS**: version and status. As a managed add-on, EKS may restore it for you; notice who fixed it |
| c12 RBAC | **EKS → Access**: *IAM* principals → Kubernetes. c12 is *in-cluster* identity (ServiceAccounts). Explain the difference |
| c14 PVC Pending | With the EBS CSI driver installed: after your fix, **EC2 → Volumes** shows a new 1 GiB gp3 volume tagged with the PVC name, in the same AZ as the node. Delete the PVC and watch the volume disappear (reclaim policy `Delete`) |

## Part 2: AWS-only failures (manual drills in the console)

Each drill: break it in the console → observe in kubectl and the console → fix → explain.
**Always undo the change at the end.**

| # | Break it | Symptom | What you learn |
|---|---|---|---|
| A1 | **EKS → Compute → workers → Edit**: desired size 0 | Every pod goes Pending; `kubectl get nodes` empties | Managed node groups and Auto Scaling groups; why `min_size` matters. Undo: desired 3 |
| A2 | **EC2 → Instances**: *Stop* one worker | Node `NotReady`; after ~5 min its pods are rescheduled; the ASG launches a replacement | Node lifecycle, zone spread, how fast EKS replaces a node. **EC2 → Auto Scaling groups → Activity** shows what happened |
| A3 | **VPC → Subnets**: remove the tag `kubernetes.io/role/elb` from all public subnets, then `kubectl create deploy web --image=nginx:1.27-alpine && kubectl expose deploy web --type=LoadBalancer --port=80` | Service `EXTERNAL-IP` stays `<pending>`; `kubectl describe svc web` says no suitable subnets | How EKS finds subnets for load balancers. Undo: re-add the tags (value `1`), delete the Service |
| A4 | **EC2 → Security groups** → the EKS **cluster security group** → *Inbound rules*: delete the rule that allows all traffic from the group itself | Pod-to-pod traffic **across nodes** times out; DNS fails for pods on nodes without CoreDNS | VPC CNI: pods use the node's security group. Undo: add back *All traffic, source = the same security group* |
| A5 | Run `./k8s-classics.sh start c14`, fix it, then `kubectl -n c14-pvc-storageclass delete pod -l app=docstore` and watch which node the new pod lands on | The pod can only run in the **same AZ** as its EBS volume | EBS volumes are zonal; `volume node affinity conflict` in a multi-AZ cluster |
| A6 | **CloudTrail → Event history**, filter *Event name* = `UpdateNodegroupConfig` (after A1) | Who changed the node group, when, from which IP | Audit trail for incident timelines: a must-have in a bank |
| A7 | **EC2 → Network interfaces**, filter by a worker's instance ID | Several ENIs per node, each with `/28` IPv4 prefixes | Where pod IPs come from (VPC CNI prefix delegation), and why subnets can run out of IPs |
| A8 | `aws eks describe-nodegroup --cluster-name bank-sim --nodegroup-name workers --query nodegroup.health` and **EKS → Compute → workers → Health issues** | (normally empty) | The first place to look when nodes don't join; you met it with the Free-plan instance error |

## Habits to build

- Start every incident with **impact and scope** (one pod, one node, one AZ, everything?),
  then pick the layer: Kubernetes object → node → AWS resource (EC2, VPC, IAM, ELB, EBS).
- Learn the console path *and* the CLI command for each check. CLI answers are faster in
  interviews and in real incidents, and the console is great for seeing relationships.
- After each drill, write two lines: *symptom → AWS root cause*. For example "Service LB pending → subnets
  missing the `kubernetes.io/role/elb` tag".

## Troubleshooting the setup

| Problem | Fix |
|---|---|
| `terraform apply` fails on `aws_eks_addon.ebs_csi` (configuration or permission) | Set `enable_ebs_csi_driver = false` in `terraform.tfvars` and apply again; c14 then needs the manual-volume path from its solution |
| `kubectl get sc` shows no `(default)` class | The EBS CSI add-on is not active: `aws eks describe-addon --cluster-name bank-sim --addon-name aws-ebs-csi-driver --query addon.status` |
| An EBS volume is left after `tf-down` | **EC2 → Volumes**, filter by tag `KubernetesCluster = bank-sim`, delete it |
