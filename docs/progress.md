# My progress

## Done

- [x] Bank simulation on EKS with eksctl. Upgraded the cluster from 1.34 to 1.37, one minor version at a time.
- [x] Corporate banking portal served from AWS Lambda (function URL with an IP allow-list, in front of an internal load balancer).
- [x] Full chain built and torn down end to end: Terraform → GitLab CI (pipeline green) → Argo CD bootstrap → merge request merged → change deployed → `make tf-down`.
- [x] Practice cluster rebuilt with Terraform: 3 nodes on 1.37, EBS CSI driver, default storage class `ebs-csi-default-sc`.

## Classic cases (`./k8s-classics.sh`)

| Case | Symptom | Root cause | Status |
|---|---|---|---|
| c01 | CrashLoopBackOff | Broken brace in a ConfigMap. Fixed it, then `rollout restart` | solved |
| c02 | ImagePullBackOff | Image tag typo `nginx:1.27-alpien` | solved |
| c03 | CreateContainerConfigError | Secret key typo `pasword`. Added `password` to the Secret and restarted the pod | diagnosed and fix given; run `check c03` next time |
| c04 | Pending (Insufficient cpu) | not started | **next** |
| c05–c16 | | | open |

**Lessons so far**
- Pod events (`describe pod`) tell you the cause in most of these cases. Read them first.
- Fix the side that broke the contract. In c03 that is the Secret, not the Deployment.
- Environment variables from a Secret are read when the container starts. Changing the Secret afterwards needs a pod restart.

## Next session

**New setup:** work from the GCP jump host, on Fargate. See `docs/jump-host-fargate.md`.

1. `cd ~/CACIB/terraform && terraform init && terraform apply` (only if the cluster was destroyed), then
   run the `update-kubeconfig` command that `terraform output` prints
2. `./k8s-classics.sh list`, then `start c04`. Look for the `FailedScheduling` event and compare the pod's CPU request with the node's allocatable CPU.
3. Continue with c05–c16, then the AWS drills in `docs/aws-hands-on.md` (A1, A3, A2, A5, A6).
4. Optional: `git push gitlab HEAD:main`. Run it on its own and enter the token at the prompt.

## End of every session

```bash
./k8s-classics.sh reset-all
YES=1 make tf-down          # undo any console drills first; about 15-20 min
```
