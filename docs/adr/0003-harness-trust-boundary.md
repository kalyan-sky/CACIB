# 0003 Harness as a read-only investigator

**Status:** accepted; 3 decisions open (below). **Date:** 2026-11-18.

## Context
Harness (`kalyan-sky/Harness_agent`) is an LLM DevOps desk with tools our agent lacks
(GitLab pipelines and job logs, Prometheus range queries, Terraform). Phase 2 lets our
agent ask it for evidence on inconclusive incidents. Our agent is read-only and advisory;
anything it calls must be too.

## Phase 0 safety review (read of the Harness code at 6dd43e5, not executed)

| # | Finding | Where | Impact |
|---|---|---|---|
| 1 | **The web desk turns write mode ON by default.** `serve` sets `allow_write = not read_only`; only `--read-only` or `DEVOPS_AGENT_READ_ONLY=true` turns it off. (The CLI and `Config` default to off, which is what the plan assumed: that was wrong for the server.) | `devops_agent/cli.py` `cmd_serve` | Must deploy with `DEVOPS_AGENT_READ_ONLY=true`, and we must check it on every call |
| 2 | **`DEVOPS_AGENT_AUTO_APPROVE` runs matching write commands with no human.** | `tickets/service.py` (fnmatch on the command) | Must be empty; we check it too |
| 3 | When write mode is off, a WRITE command is **refused** with an error (not queued for approval). Unknown binaries are BLOCKED; only `kubectl`, `terraform`, `helm` run. | `tools/base.py`, `safety.py` | Good: matches our model |
| 4 | Classification is by argv: anything not provably read-only is WRITE. Secret contents (`get secret -o ...`) count as WRITE; `exec`, `port-forward`, `run`, `debug`, `cp` are WRITE. | `safety.py` | Good, but a classifier is not a security boundary on its own |
| 5 | HTTP tools (GitLab, Prometheus, Grafana) are not classified; they call fixed endpoints. One POST: GitLab `ci/lint` (no side effects). | `tools/gitlab.py` | Acceptable; GitLab token must be `read_api` only |
| 6 | `/api/config` reports `write_mode` and `auto_approve`, behind the UI token. | `web/server.py` | We can verify both before each request |

## Decision
Defence in depth, three independent layers. Any one of them alone stops a write:
1. **Harness config:** `DEVOPS_AGENT_READ_ONLY=true`, no `DEVOPS_AGENT_AUTO_APPROVE`.
2. **Our trust check:** before each ticket our agent reads `/api/config` and refuses to send
   anything unless `write_mode == false` and `auto_approve == []` (contract test in Phase 2).
3. **Credentials that cannot write:** Harness's ServiceAccount bound to our read-only
   ClusterRole (no Secrets, no exec); GitLab token `read_api`; no AWS write permissions.
   Even a classifier bug or a prompt injection in logs cannot change anything.

Harness changes (own branch, Phase 2): investigate-only ticket mode, an `evidence` field in
`report_resolution`, and `allow_write` named explicitly in `/api/config`.

## Open decisions (needed before Phase 2)
1. Where Harness runs: **in the EKS cluster** (recommended) or Cloud Run.
2. LLM: OpenRouter key and a monthly budget (entered through a Secret, never in chat).
3. Your OK to make the small Harness changes above on their own branch of the Harness repo.
