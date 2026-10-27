# Terraform + GitLab CI + Argo CD: the full end-to-end

This replaces the `eksctl` + `kubectl apply` setup with the toolchain most
platform teams run:

```
 you ──git push──► GitLab
                    ├─ CI test:    Go + Python tests, render all 216 apps, platform audit, schema check
                    ├─ CI build:   image → GitLab Container Registry   (no Docker daemon, no AWS keys)
                    └─ CI publish: rendered manifests for that image → branch `gitops`
                                                                        │ Argo CD polls (3 min)
 Terraform ──► AWS: VPC (3 AZs) + EKS + node group + add-ons            ▼
                     └──────────────────────────────────────► Argo CD in EKS ──sync──► the bank
```

- **Terraform** owns the AWS infrastructure (`terraform/`).
- **GitLab CI** owns *what* gets deployed (`.gitlab-ci.yml`, `ci/`).
- **Argo CD** owns *deploying it* (`gitops/argocd/`).

The pipeline never touches the cluster and stores no cloud credentials. This
"pull-based GitOps" is what banks like: deployment rights sit with Argo CD inside
the cluster, and every change is a reviewed Git commit.

**Time:** about 2 hours the first time. **Cost:** about $0.45/hour while EKS runs, from your
Free plan credits. Everything is in CloudShell; `<you>` below is your GitLab username.

---

## Step 1: Delete the current eksctl cluster

```bash
export PATH=$HOME/bin:$PATH AWS_REGION=ap-southeast-2
cd ~/CACIB && git pull

./scripts/portal-lambda.sh down                                  # only if you created the Lambda portal
kubectl -n sim-traffic delete svc dashboard-lb --ignore-not-found # only if you created the dashboard LB
YES=1 make eks-down                                               # 10-15 min

# check nothing is left
eksctl get cluster --region $AWS_REGION                           # "No clusters found"
aws cloudformation list-stacks --region $AWS_REGION \
  --query "StackSummaries[?starts_with(StackName,'eksctl-bank-sim') && StackStatus!='DELETE_COMPLETE'].StackName"   # []
```

## Step 2: Install Terraform in CloudShell

```bash
TFV=1.16.7
curl -sSLO https://releases.hashicorp.com/terraform/${TFV}/terraform_${TFV}_linux_amd64.zip
unzip -o -q terraform_${TFV}_linux_amd64.zip terraform -d ~/bin && rm terraform_${TFV}_linux_amd64.zip
terraform version
```
(`~/bin` survives CloudShell restarts; tools installed with `dnf` don't.)

**CloudShell disk:** your home directory holds only 1 GB, and the AWS provider alone is
several hundred MB. Keep Terraform's working files in `/tmp`, which is larger but wiped at
the end of a session; the state file stays in your home folder:
```bash
echo 'export TF_DATA_DIR=/tmp/terraform-data' >> ~/.bashrc
export TF_DATA_DIR=/tmp/terraform-data
rm -rf ~/aws ~/awscliv2.zip ~/.cache/go-build      # installer leftovers and caches, if present
```
In a new CloudShell session, run `terraform init` again before other Terraform commands.

## Step 3: GitLab project, token and first pipeline

1. Sign in at **gitlab.com**. New accounts must **verify their identity** (phone or card)
   before they can use GitLab's free CI runners. Without it, jobs stay `pending`.
2. **New project → Create blank project**: name `cacib`, visibility **Public**, and
   *untick* "Initialize repository with a README".
   *Public* lets Argo CD read the repo and EKS pull the images without credentials. The code
   and data are fictional. (A private repo needs a deploy token in Argo CD and an image pull
   secret in every namespace; see troubleshooting.)
3. **Avatar → Edit profile → Access tokens → Add new token**: name `cacib-ci`, expiry ≤ 30 days,
   scope **`write_repository`**. Copy the `glpat-…` value.
4. In the project: **Settings → CI/CD → Variables → Add variable**:
   key `GITOPS_TOKEN`, value = the token, **Masked**, Protect = on.
5. Push the code to GitLab as `main`:
   ```bash
   cd ~/CACIB
   git remote add gitlab https://gitlab.com/<you>/cacib.git
   git config credential.helper 'cache --timeout=7200'
   git push gitlab HEAD:main        # username: <you>   password: the glpat-… token
   ```
6. **Build → Pipelines**: six jobs should go green in ~5 minutes:
   `go-test, python-and-scripts, manifests, schemas → build-image → publish-gitops`.
   Then check that:
   - **Deploy → Container Registry** shows `txn-service` with a short-SHA tag and `latest`.
   - **Code → Branches** has `gitops`; open `deploy/eks/` to see what Argo CD will deploy.

## Step 4: Build the infrastructure with Terraform

```bash
cd ~/CACIB/terraform
cp terraform.tfvars.example terraform.tfvars    # region = ap-southeast-2, instance_type = m7i-flex.large
terraform init
terraform plan -out tfplan                      # read it: "Plan: 30 to add, 0 to change, 0 to destroy" (16 network, 7 IAM, 7 EKS/nodes)
terraform apply tfplan                          # 15-20 min; press a key now and then so CloudShell stays open
terraform output
```
What it builds and why, file by file:
| File | Contents |
|---|---|
| `vpc.tf` | VPC over 3 AZs; public subnets for nodes, private subnets for internal LBs and Lambda; NAT optional (off) |
| `iam.tf` | cluster role and node role. No IAM OIDC provider, which your organization blocks |
| `eks.tf` | EKS 1.37 with API access entries, STANDARD support; vpc-cni (network policy + prefix delegation) and kube-proxy **before** the nodes, then 3 × m7i-flex.large via a launch template (110 pods/node, gp3, IMDSv2); coredns + metrics-server after |

If CloudShell drops during `apply`, open it again, `cd ~/CACIB/terraform`, run
`terraform apply` again, and it continues. **Never delete `terraform.tfstate`**: it's the
only record of what Terraform built.

## Step 5: Install Argo CD and let it deploy the bank

```bash
cd ~/CACIB
REPO_URL=https://gitlab.com/<you>/cacib.git ARGOCD_UI_IP=112.199.221.112 make gitops-bootstrap
```
The script does five things:
1. Connects kubectl (`eks-bank-sim`).
2. Installs **Argo CD v3.5.5**.
3. Creates the `bank-sim` **AppProject**, which limits what may be deployed, from where and to where.
4. Creates the `bank-sim` **Application**, which watches branch `gitops`, path `deploy/eks`.
5. Waits for `Synced / Healthy` and prints the Argo CD UI address and admin password.

```bash
kubectl -n argocd get application bank-sim     # SYNC STATUS Synced, HEALTH STATUS Healthy
./sim.sh status                                # 56/56 healthy, customer flows HEALTHY
make audit                                     # all controls PASS
```
Open the printed `https://…elb.amazonaws.com` (accept the self-signed certificate warning),
log in as `admin`, and open the **bank-sim** app to see all ~250 objects as a tree.

## Step 6: The GitOps workflow (do this at least once)

**Ship a change through review.** Onboard the new app from ticket 09:
```bash
git switch -c onboard-fx-options
# edit inventory/applications.yaml (see scenarios/09-onboard-fx-options-pricer/solution.md)
git commit -am "Onboard fx-options-pricer (RITM0072301)"
git push gitlab onboard-fx-options              # GitLab prints a "create merge request" link
```
1. Open the merge request. The pipeline runs only the **tests**, so a broken inventory or a
   failing control can't be merged.
2. Merge it. The `main` pipeline builds, then `publish-gitops` commits the new manifests.
3. Argo CD notices within 3 minutes. Press **Refresh** in the UI to make it immediate.
4. Watch `fx-options-pricer` appear, and `corporate-portal-api` roll out with its new config hash.

**Roll back.** In GitLab open branch `gitops` → the latest commit → **Revert**, then commit to
`gitops`. Argo CD deploys the previous state. Alternatively, `git revert` the change on `main`
so the pipeline publishes the old state. In an interview, explain why both work and why the
second keeps `main` and `gitops` consistent.

**See drift.** Run `./sim.sh start 01`. Argo CD shows `bank-sim` **OutOfSync**, and **App Diff**
shows exactly which key was removed from the ConfigMap. `selfHeal` is off so the incidents stay
broken until you fix them. Turn it on to see GitOps undo manual changes automatically:
```bash
kubectl -n argocd patch application bank-sim --type merge -p '{"spec":{"syncPolicy":{"automated":{"selfHeal":true}}}}'
```

## Step 7 (optional): The portal on Lambda

```bash
make portal-lambda IP=112.199.221.112
```

## Step 8: End of the day: delete everything

```bash
cd ~/CACIB && YES=1 make tf-down     # Lambda + load balancers first, then terraform destroy (15-20 min)
```
GitLab keeps the code, pipelines and images, which are free. Tomorrow: `terraform apply` +
`make gitops-bootstrap` gives you the same bank back in ~25 minutes, because Git is the
source of truth.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| GitLab jobs stay **pending** | Account not verified for the free shared runners. GitLab shows a banner on the pipeline page asking you to verify (phone or card); finish it, then retry. |
| `publish-gitops`: *Set the masked CI/CD variable GITOPS_TOKEN* | Add the variable (Step 3.4), then **Retry** the job. |
| `publish-gitops`: `403` on push | Token lacks `write_repository`, or has expired. |
| Argo CD: `ComparisonError … authentication required` | The project is private. Make it public, or add credentials: `argocd repo add https://gitlab.com/<you>/cacib.git --username <you> --password <read_repository token>` |
| Pods `ImagePullBackOff` from `registry.gitlab.com` | The registry is private. **Settings → General → Visibility → Container registry: Everyone with access**. |
| Argo CD: `resource :Secret is not permitted in project bank-sim` | The AppProject guardrail is working: something other than the platform's rendered kinds reached the gitops branch. |
| `terraform apply`: `AccessDenied … service control policy` | Your organization's Free plan rules. Paste the error; the config avoids the known ones (OIDC, non-free instance types). |
| Node group never gets nodes | `instance_type` isn't free-tier eligible: `aws ec2 describe-instance-types --filters Name=free-tier-eligible,Values=true --query 'InstanceTypes[].InstanceType'` |
| `terraform init`: `no space left on device` | CloudShell's 1 GB home is full. Set `TF_DATA_DIR=/tmp/terraform-data` (Step 2) and remove installer leftovers |
| `terraform destroy` stuck on the VPC or subnets | Something outside Terraform still has a network interface in the VPC (a load balancer, the Lambda). Use `make tf-down`, which removes those first. |

## Interview talking points

- **Separation of duties:** CI can't deploy, and Argo CD can't build. A compromised pipeline can
  only propose a commit to `gitops`, and the AppProject limits what that commit may contain.
- **Rendered-manifests pattern:** the `gitops` branch is *exactly* what runs, so diffs, audits and
  rollbacks are Git operations.
- **Terraform state** is the crown jewel. In a team it goes to a remote backend with locking and
  encryption (S3 with `use_lockfile`, or GitLab-managed state), with `plan` in the merge request
  and a manual `apply` job.
- **Why not one Terraform for everything:** Argo CD is installed by a bootstrap step, not by the
  same Terraform that creates the cluster. That avoids the "Kubernetes provider before the
  cluster exists" problem, and keeps the infrastructure and application lifecycles separate.
- **Upgrades as code:** raise `kubernetes_version` by one minor version and `terraform apply`. The control
  plane goes first, then the node group (`version` follows the cluster), one node at a time
  (`max_unavailable = 1`), honouring PodDisruptionBudgets.
