# 0002 Durable history with Litestream

**Status:** accepted, built in Phase 1. **Date:** 2026-11-18.

## Context
The agent's SQLite database holds what makes it useful over time: incidents, RCAs,
approvals, the audit log, and engineer verdicts (accuracy, "seen before"). It lived on an
EBS volume of the cluster, so `make tf-down` erased it. In a bank, the audit trail of
who approved what must outlive any one cluster.

## Decision
- Keep SQLite (one writer, simple, fast) and stream it with **Litestream** to a replica
  **outside the cluster's lifecycle**. `bin/start` restores before the agent starts and
  runs the agent under `litestream replicate`.
- The replica is a URL, so the same image fits every site:
  AWS S3 (lab, cloud), S3-compatible MinIO/Ceph (on-prem), `file:///` on a shared
  filesystem (HPC: GPFS/Lustre/NFS), GCS, Azure, SFTP.
- Lab: S3 bucket in its own Terraform stack (`terraform/history`, own state) so
  `tf-down` cannot delete it. Versioned, SSE-S3, public access blocked, TLS-only policy,
  old versions expire after 30 days.
- Credentials: **EKS Pod Identity** (needs no IAM OIDC provider, which the account's SCP
  blocks), scoped to `sre-agent/<cluster>/` and to the `sre-agent` ServiceAccount only.
  Fallback `agent_history_access = "node_role"` if Pod Identity is also blocked.
- **Fail closed:** if the restore fails, the pod does not start (no silent fresh start
  that would hide the loss). `SRE_AGENT_REPLICA_REQUIRED=false` overrides.
- Alerts: `SreAgentHistoryReplicaDown`, `SreAgentHistoryReplicationErrors`.

## Consequences
- One replica per database: still exactly one agent replica (as before). Active/active
  needs a server database (PostgreSQL); revisit when there are several agents per site.
- Restore drill: `./scripts/sre-agent-history.sh check` (restores into a scratch file in
  the pod and compares row counts). Run it after each rebuild.
- Production: SSE-KMS with a customer key, bucket in a separate account with Object Lock
  (WORM) if the audit log is a record under retention rules.
