# CACIB lab: bank simulation on EKS + read-only SRE agent

Instructions for AI coding agents (and people) working in this repo.

## Non-negotiable rules (the SRE agent)
1. **The agent never changes anything.** No write verbs in `apps/sre-agent/deploy/rbac.yaml`,
   no new operation in `k8s/readonly.py` `ALLOWED_OPERATIONS` that mutates, no code path that
   executes a fix. Fixes are text for an engineer; approval only records a decision.
2. **No Secret values, ever.** The agent cannot read Secrets (RBAC); never add it. All text that
   leaves the agent goes through `redact.py`.
3. **Evidence over guesses.** A finding states its source. Rules outrank the LLM and external
   agents (Harness); those are labelled external and never raise confidence on their own.
4. **No credentials in code, docs, chat or commits.** Scripts read them through hidden prompts
   into Kubernetes Secrets.
5. **Don't claim it works without a test.** Live behaviour is verified on the jump host by the
   user; say plainly what was and wasn't run.

## Map
- `terraform/` EKS (ec2 or fargate), VPC, IAM; `terraform/history/` the agent's history
  bucket (own state, survives `make tf-down`). Account SCP blocks IAM OIDC: use EKS Pod
  Identity or the node role, never IRSA.
- `apps/sre-agent/` the agent: `sre_agent/` code, `bin/start` (Litestream wrapper),
  `deploy/` manifests, `tests/`.
- `scripts/` jump-host operations (`sre-agent-up.sh`, `sre-agent-verify.sh`,
  `sre-agent-history.sh`, `cert-lab.sh`, `tf-down.sh`); `platform/monitoring/` alert rules.
- `docs/adr/` decisions (read 0001 before adding a platform or analyzer);
  `docs/progress.md` what the user has verified live.

## Commands
- `make sre-agent-test` all agent tests (offline, about 10 s). Must pass before any commit.
- `make test` repo-wide offline checks (Go, render, audit, script syntax).
- Terraform: `terraform fmt -recursive terraform` and `terraform validate` in both stacks.

## Recipes
- New incident type: use the `add-incident-type` skill (`.claude/skills/`).
- Before committing agent changes: run the `safety-reviewer` subagent on the diff.
- New platform (e.g. Slurm) or analyzer (LLM, Harness): follow ADR 0001 contracts.

## Conventions
- Lab runs on a GCP jump host (`air-gap-singapore`), AWS Free plan, region from tfvars.
- Scripts: `source scripts/lib.sh`; use `k`, `info`, `ok`, `ko`, `warn`, `die`, `check_result`.
- Commands shown to engineers must run as printed: no `<placeholders>`; ask with `read -rp`.
