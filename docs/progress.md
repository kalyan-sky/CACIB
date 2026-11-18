# My progress

## Done

- [x] Bank simulation on EKS with eksctl. Upgraded the cluster from 1.34 to 1.37, one minor version at a time.
- [x] Corporate banking portal served from AWS Lambda (function URL with an IP allow-list, in front of an internal load balancer).
- [x] Full chain built and torn down end to end: Terraform → GitLab CI (pipeline green) → Argo CD bootstrap → merge request merged → change deployed → `make tf-down`.
- [x] Practice cluster rebuilt with Terraform: 3 nodes on 1.37, EBS CSI driver, default storage class `ebs-csi-default-sc`.
- [x] GCP VM `air-gap-singapore` set up as the jump host (static IP 34.87.82.120). The EKS API allowed only that IP; CloudShell could not reach it.
- [x] EKS on Fargate with Terraform (`compute = "fargate"`): Fargate profiles, CoreDNS on Fargate, NAT gateway. Destroyed afterwards.
- [x] Full bank on EC2 nodes through the jump host: Argo CD (bank-sim + monitoring apps), Prometheus/Grafana with the bank dashboard.
- [x] Read-only SRE agent live (`make sre-agent-up`): 25/25 RBAC can-i checks passed, Alertmanager routes to it, the c07 OOMKilled test incident was investigated and the email arrived in Gmail.
- [x] RCA workflow and alert coverage added (draft -> RCA for review email -> engineer sign-off -> final RCA).
- [x] Certificate monitoring live (x509 exporter + blackbox probes): cert-lab wrong-hostname incident detected, emailed with a guide,
      fixed by hand (new CSR with the right names, lab CA, Secret replaced, restart), verified with openssl (return code 0), RCA email received.
- [x] Ready-to-run commands + fix scripts: c07 OOMKilled fixed with the agent's script. Second certificate run:
      both certificates (expiring + wrong hostname) fixed and verified at every layer (Secret, client handshake,
      alerts resolved, incidents resolved); both RCAs signed off. Live runs found two guide bugs, both fixed:
      the fix script must wait for new.crt at the CA step, and exporter-only alerts must use the certificate's hostname.
- [x] Learning from past incidents live: the repeat c07 email showed "Seen before" (earlier incident, its fix and
      time to resolve); RCA sign-off records the agent's accuracy; the accuracy page shows the scorecard.

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
5. **SRE agent history (Phase 1, built, not yet run live):** `make history-up`, `make tf-up`,
   `make sre-agent-up`, then `./scripts/sre-agent-history.sh status` and `check`.
   Done when the dashboard shows the old incidents after a `tf-down` / `tf-up`.
   Guide: `docs/sre-agent-history.md`. Decisions: `docs/adr/`.

## End of every session

```bash
./k8s-classics.sh reset-all
YES=1 make tf-down          # on the jump host; undo any console drills first; about 15-20 min
aws eks list-clusters --region ap-southeast-2    # []
```
