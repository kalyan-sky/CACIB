# My progress

## Done

- [x] Bank simulation on EKS with eksctl. Upgraded the cluster from 1.34 to 1.37, one minor version at a time.
- [x] Corporate banking portal served from AWS Lambda (function URL with an IP allow-list, in front of an internal load balancer).
- [x] Full chain built and torn down end to end: Terraform → GitLab CI (pipeline green) → Argo CD bootstrap → merge request merged → change deployed → `make tf-down`.
- [x] Practice cluster rebuilt with Terraform: 3 nodes on 1.37, EBS CSI driver, default storage class `ebs-csi-default-sc`.
- [x] GCP VM `air-gap-singapore` set up as the jump host (static IP 34.87.82.120). The EKS API allowed only that IP; CloudShell could not reach it.
- [x] EKS on Fargate with Terraform (`compute = "fargate"`): Fargate profiles, CoreDNS on Fargate, NAT gateway. Destroyed afterwards.

## Classic cases (`./k8s-classics.sh`)

| Case | Symptom | Root cause | Status |
|---|---|---|---|
| c01 | CrashLoopBackOff | Broken brace in a ConfigMap. Fixed it, then `rollout restart` | solved |
| c02 | ImagePullBackOff | Image tag typo `nginx:1.27-alpien` | solved |
| c03 | CreateContainerConfigError | Secret key typo `pasword`. Added `password` to the Secret and restarted the pod | diagnosed and fix given; run `check c03` next time |
| c04 | Pending (Insufficient cpu) | On Fargate: "exceeds the max Fargate configuration". The request was 64 CPUs against 50m measured; set 100m/500m. Fargate billed 0.25vCPU 0.5GB | solved (14m05s) |
| c07 | OOMKilled | not started | **next** |
| c05, c06, c08–c16 | | | open (c05, c06 and c14 need `compute = "ec2"`) |

**Lessons so far**
- Pod events (`describe pod`) tell you the cause in most of these cases. Read them first.
- Fix the side that broke the contract. In c03 that is the Secret, not the Deployment.
- Environment variables from a Secret are read when the container starts. Changing the Secret afterwards needs a pod restart.

## Next session

Work from the GCP jump host (`cd ~/CACIB && git pull`). See `docs/jump-host-fargate.md`.
Choose `compute` in `terraform/terraform.tfvars`: `"fargate"` for the classics, `"ec2"` for the full bank (80+ pods, Argo CD).

1. `cd ~/CACIB/terraform && terraform plan -out tfplan && terraform apply tfplan` (inside `tmux`), then
   `$(terraform output -raw configure_kubectl)`
2. Classics: `./k8s-classics.sh start c07`. Bank: `REPO_URL=https://gitlab.com/kalyansiddam0/cacib.git ARGOCD_UI_IP=<laptop IP> make gitops-bootstrap`.
3. Continue with c05–c16, then the AWS drills in `docs/aws-hands-on.md` (A1, A3, A2, A5, A6).
4. Optional: `git push gitlab HEAD:main`. Run it on its own and enter the token at the prompt.

## End of every session

```bash
./k8s-classics.sh reset-all
YES=1 make tf-down          # on the jump host; undo any console drills first; about 15-20 min
aws eks list-clusters --region ap-southeast-2    # []
```
