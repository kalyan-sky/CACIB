# Architecture decisions

Short records of decisions that shape the SRE agent, so later work (and later reviewers)
know why things are the way they are. One file per decision; supersede, don't rewrite.

| # | Decision | Status |
|---|---|---|
| [0001](0001-agent-framework-and-platforms.md) | One agent framework, platform adapters (Kubernetes now, HPC/Slurm next) | Accepted (refactor in Phase 1.5) |
| [0002](0002-durable-history.md) | Durable history with Litestream, replica outside the cluster | Accepted, built (Phase 1) |
| [0003](0003-harness-trust-boundary.md) | Harness as a read-only investigator behind a trust check | Accepted; 3 decisions open |
