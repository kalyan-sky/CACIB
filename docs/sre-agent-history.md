# SRE agent history that survives `make tf-down`

The agent's database (incidents, RCAs, approvals, audit log, accuracy verdicts) is copied
continuously to S3 by Litestream and restored when the agent starts on a new cluster.
Why and how: [ADR 0002](adr/0002-durable-history.md).

```
agent pod ── SQLite on EBS (/data) ── Litestream ──► s3://<bucket>/sre-agent/<cluster>/
             bin/start: restore if /data is empty, then replicate every change (~1 s)
```

## Turn it on (jump host, once)
```bash
cd ~/CACIB && git pull
make history-up      # S3 bucket (own Terraform state) + writes agent_history_bucket into terraform.tfvars
make tf-up           # adds the EKS Pod Identity agent, an IAM role scoped to the bucket prefix, the association
SMTP_USER=you@gmail.com make sre-agent-up   # new image with Litestream; replication on
./scripts/sre-agent-history.sh status       # replica, start-up line, errors, objects in S3
```
Cluster already running? Same steps: `make tf-up` only adds the new pieces (about 2 min).

## Prove it works
1. **Restore drill (non-destructive):** `./scripts/sre-agent-history.sh check` restores the
   replica into a scratch file inside the pod and compares row counts with the live database.
2. **The real test:** note the incident count on the dashboard, then `YES=1 make tf-down`
   (it stops the agent first, so the last changes are flushed), `make tf-up`,
   `make monitoring`, `make sre-agent-up`. The dashboard shows the old incidents, RCAs and
   the accuracy page as before; `status` says "history restored from the replica".

## What it costs
A few MB in S3 Standard: well under USD 0.10 a month. The bucket stays when the cluster
goes. To delete it and all history: `./scripts/sre-agent-history.sh destroy`.

## If Pod Identity is blocked
If `make tf-up` fails on the Pod Identity association or the pod logs show S3
`AccessDenied`/no credentials, set `agent_history_access = "node_role"` in
`terraform/terraform.tfvars` and run `make tf-up` again. That gives the bucket-prefix policy
to the node role instead (every pod on the nodes could use it: fine for the lab only).

## Behaviour to know
| Situation | What happens |
|---|---|
| New cluster, replica exists | restored before the agent starts |
| First ever start, no replica | starts empty, replicates from then on |
| Pod restarts, EBS volume kept | local database kept (it is the newest), replication continues |
| Restore fails (credentials, network) | **pod does not start** and logs why: the history is never silently lost. Override: `SRE_AGENT_REPLICA_REQUIRED=false` in the ConfigMap |
| Replication stops or errors | alerts `SreAgentHistoryReplicaDown` / `SreAgentHistoryReplicationErrors` reach the agent itself |

## Other sites (on-prem, HPC)
`SRE_AGENT_REPLICA_URL` takes any Litestream replica: `s3://bucket/path?endpoint=https://minio.internal:9000`
(on-prem S3), `file:///gpfs/ops/sre-agent` (shared filesystem), `gs://`, `abs://`, `sftp://`.
`bin/start` works the same in Kubernetes, Podman or a systemd unit.
