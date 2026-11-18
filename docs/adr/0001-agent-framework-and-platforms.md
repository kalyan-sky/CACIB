# 0001 One agent framework, platform adapters (Kubernetes now, HPC next)

**Status:** accepted; refactor scheduled as Phase 1.5 (before Harness in Phase 2).
**Date:** 2026-11-18.

## Context
The agent is built for Kubernetes. The goal is a product that is production-ready for
**HPC** too (assumed here: Slurm-scheduled compute and GPU clusters, as banks run for risk
grids such as Monte Carlo, VaR and XVA, with shared filesystems like GPFS or Lustre).
More analyzers are coming (LLM, Harness). Without a framework, each platform and each
analyzer would be wired into the pipeline by hand, and the safety rules would be
re-implemented each time.

## Decision: a small framework with eight parts and fixed contracts

```
 Signals ─► Intake ─► Platform adapter ─► Analyzers ─► Arbiter ─► Report ─► Human gate
 (alerts)  (normalize) (read-only tools,   (rules, run- (rules beat  (email,    (approve /
                        evidence)           books, LLM,  LLM; labels  dashboard, reject; 2
                                            Harness)     sources)     RCA)       approvers)
                 └──────────── Guardrails (allowlist, redaction, budgets) ──────────┘
                 └──────────── Memory (store + replica) · Evaluation (verdicts) ────┘
```

| Part | Contract | Today (Kubernetes) | HPC |
|---|---|---|---|
| **Intake** | any signal → `NormalizedAlert` (platform, site, target, severity) | Alertmanager webhook | the same Alertmanager webhook: slurm-exporter, DCGM (GPU), node, Lustre/GPFS exporters |
| **Platform adapter** | `collect(alert, deadline) → Evidence`; declares its allowlisted read-only operations | `diagnostics.py` + `k8s/readonly.py` | Slurm: `sinfo`, `squeue`, `scontrol show node/job`, `sacct`, `sdiag` via slurmrestd (GET only) or an allowlisted SSH runner; GPU via DCGM; filesystem via `lfs df` / `mmhealth` |
| **Analyzer** | `analyze(evidence) → Finding` {type, cause, confidence, evidence refs, source} | `analysis/rules.py`, `runbooks.py`, `llm.py` | Slurm rules: node drained (Reason), GPU Xid / ECC errors, job OOM (cgroup), pending reasons (`ReqNodeNotAvail`, `QOSMaxJobsPerUser`), scratch full, OST down, `slurmctld` slow |
| **Arbiter** | merge findings; deterministic rules outrank LLM and external agents; external findings never raise confidence on their own | inside `pipeline.py` | unchanged |
| **Guardrails** | every tool call passes one allowlist; redaction on all text; time and token budgets; a test fails the build if any adapter exposes a write | `ReadOnlyKube`, `redact.py` | the same contract for the Slurm runner (no `scontrol update`, `scancel`, `sbatch`) |
| **Human gate** | proposed changes are text; approval records a decision and never executes | approvals, audit log | the same, with Slurm commands in the fix script (`scontrol update NodeName=... State=RESUME` for an engineer to run) |
| **Memory** | store + durable replica + similar incidents | `store.py`, Litestream (ADR 0002) | replica on GPFS/Lustre (`file:///`) or on-prem MinIO |
| **Evaluation** | scenario tests per adapter; engineer verdicts; calibration | `tests/`, accuracy page | Slurm scenario fixtures (recorded command output) |

The **report, RCA, fix script, accuracy and approvals stay platform-neutral**: an HPC
incident gets the same layout and the same human gate as a Kubernetes one.

## Production on HPC sites (what changes, what doesn't)
- Runs **without Kubernetes**: the same container under Podman or a systemd unit on an
  admin node; `bin/start` already works there (replica on a shared filesystem).
- **Air-gapped** by default: LLM off or an on-prem model; no internet egress needed.
- **Identity:** a dedicated read-only service account; Slurm `AdminLevel=None` and no jobs
  of its own; slurmrestd behind a proxy that allows GET only (a normal Slurm user can
  still cancel its own jobs, so the API path must not allow it at all).
- **Scale:** HPC alerts come in storms (a rack loses power: 400 nodes down at once). The
  intake groups by failure domain (rack, switch, filesystem) and investigates once per group.
- **Batch windows:** business alerts such as "end-of-day risk batch late" link to the
  infrastructure findings that explain them.

## Consequences
- Phase 1.5 refactor (about 3 days): introduce the `PlatformAdapter` and `Analyzer`
  interfaces, move the Kubernetes code behind them unchanged, keep all current tests green,
  and add a contract test that every adapter's operations are read-only.
- Harness (Phase 2) and the LLM (Phase 3) become **analyzers** that plug in, not special cases.
- Slurm adapter (new Phase 7): fixtures and rules first, then a live test on a small
  Slurm cluster (e.g. AWS ParallelCluster in the lab).
