# Read-only Kubernetes SRE agent

An investigator and advisor, **not** a remediation bot. Alertmanager sends it alerts; it
collects evidence with read-only API calls, explains the probable root cause with cited
evidence, writes a step-by-step resolution guide, emails the report, and records the proposed
fix for a human to approve. Approving changes nothing in the cluster: an engineer runs the
commands.

```
Prometheus (kube-prometheus-stack + platform/monitoring/k8s-incident-rules.yaml)
   │ alerts
Alertmanager ── group, dedupe, inhibit ──► POST /api/v1/alerts?cluster=<name>  (Bearer token)
                                                   │
                     sre-agent (namespace sre-agent, read-only ServiceAccount)
   ┌──────────────────────────────────────────────┼───────────────────────────────────────────┐
   │ intake: validate, correlate by workload/node, persist the incident FIRST (SQLite on EBS)   │
   │ diagnostic engine: allowlisted GETs only (pods, logs, events, deployments, replicasets,    │
   │   nodes, metrics, services/endpoints, PVCs, storage classes, quotas), limits + redaction   │
   │ root cause: rule engine (default)  ──optional──► LLM (anthropic | openai | bedrock),       │
   │   answer validated, evidence grounded, invariants re-applied, failure -> rules report      │
   │ report: schema JSON + guide (diagnostic / remediation NOT executed / verification / rollback)│
   │ approval record (Pending → Approved | Rejected | Expired) + audit log                      │
   │ email (Gmail SMTP, HTML, per-severity/environment recipients, dedup, retry)                 │
   │ dashboard + JSON API (Basic auth, approver/viewer roles, CSRF), /metrics, JSON logs        │
   └────────────────────────────────────────────────────────────────────────────────────────────┘
```

## What existed and what was added

| Area | Before | Now |
|---|---|---|
| Prometheus, Alertmanager, Grafana | Installed by Argo CD (`make monitoring`); Alertmanager had no receiver | Alertmanager routes to the agent (grouping, dedup, inhibition); 17 extra incident rules |
| Python service | none (only `tools/platform.py`) | `apps/sre-agent/` FastAPI app, 44 tests |
| RBAC for automation | team RoleBindings only | `sre-agent-readonly`: get/list/watch, no Secrets/ConfigMaps/exec/writes |
| Email, AI, approvals, incident UI | none | added; LLM off by default (your choice: rule engine) |
| Image builds | crane for the Go app | `ci/build-sre-agent.sh`: crane, no Docker; pushes to ECR from the jump host |

## Install (on the jump host)

Prerequisites: the EKS cluster with `compute = "ec2"`, `make gitops-bootstrap` and `make monitoring` done.

1. **Gmail App Password**: Google account → Security → 2-Step Verification on → **App passwords** →
   create one named `sre-agent`. It is 16 letters. You'll paste it once; it goes only into a Kubernetes Secret.
2. pip for the image build: `sudo apt-get install -y python3-pip`
3. Install:
   ```bash
   cd ~/CACIB && git pull
   make sre-agent-up SMTP_USER=kalyansiddam0@gmail.com EMAIL_TO=kalyansiddam0@gmail.com DASHBOARD_IP=<laptop IP>
   ```
   It asks for the App Password and a new dashboard password, builds the image (crane, ~2 min),
   pushes it to ECR, deploys the agent, adds the alert rules, routes Alertmanager to it, and runs the
   verification below. Without `DASHBOARD_IP`, open the dashboard with
   `kubectl -n sre-agent port-forward svc/sre-agent 8080` on the jump host.
4. Verify on the live cluster, including a real test incident:
   ```bash
   make sre-agent-verify ARGS=--incident
   ```
   Expected: every `can-i` line ✔ (reads **yes**; delete/patch/exec/secrets/escalate **no**), the agent
   healthy, Alertmanager routing to it, then `INC-000N OOMKilled confirmed ... email sent` within ~6 minutes,
   and the email in your inbox. Then `./k8s-classics.sh reset c07`.

## Using it

- **Email** subject: `[CRITICAL] Kubernetes Incident Detected - bank-sim - c07-oomkilled`. It contains the
  root cause with confidence, the evidence it saw, every read-only command it ran, the labelled resolution
  guide, risk, rollback and the approval status.
- **Dashboard** (`https://<dashboard LB>`, self-signed certificate): totals, critical/high/active/resolved,
  pending approvals, cluster and namespace health, filters (cluster, namespace, severity, type, status, dates),
  and a page per incident with the full evidence, logs, report JSON, approval form and audit log.
- **Approve or reject** on the incident page (approver role). It only records the decision, with your name
  and time, in the audit log. Run the approved commands yourself, through Git where the workload is GitOps-managed.
- **API** (same Basic auth): `GET /api/v1/incidents?severity=CRITICAL`, `GET /api/v1/incidents/INC-0001`,
  `GET /api/v1/approvals?status=Pending`, `POST /api/v1/approvals/{id}/approve {"comment": ""}`,
  `POST /api/v1/approvals/{id}/reject {"reason": "..."}`.

Practise with the classics: `./k8s-classics.sh start c02` (ImagePullBackOff), `c03` (missing Secret key),
`c04` (Pending), `c07` (OOMKilled), `c14` (PVC), `c16` (CoreDNS). Compare the agent's diagnosis with yours.

## What it detects (rule engine)

CreateContainerConfigError (missing Secret/ConfigMap key or object) · ImagePullBackOff (missing tag, registry
auth, network) · OOMKilled · liveness probe kills · CrashLoopBackOff (exit code meaning + the decisive log line:
bad config, missing env, dependency down, permissions, missing file, crash) · PVC Pending (missing/no default
StorageClass) · ResourceQuota exceeded · Pending (insufficient CPU/memory, selector, taint, PVC, Fargate size,
too many pods) · readiness failures (404, refused, timeout) · Service without endpoints · CPU throttling ·
node NotReady and Memory/Disk/PID pressure (with EC2/ASG checks) · CoreDNS down. Anything else:
**"Root cause not conclusively identified. Additional investigation is required."** with next steps.

Confirmed facts go in `supporting_evidence` with their source; anything inferred is listed under
`possible_contributing_factors` and marked *(hypothesis)*.

## Security model

| Control | How |
|---|---|
| Read-only | ClusterRole with get/list/watch only; the code has an operation allowlist with no write, exec or Secret call (tested statically and on the wire) |
| No Secret contents | Secrets and ConfigMaps are not in the ClusterRole; env values with secret-like names, `last-applied` annotations, tokens, keys, passwords and URL credentials are redacted before storage, email or LLM |
| No auto-remediation | No code path executes commands; approval only writes a record (tested: zero API calls after approval) |
| No privilege escalation | No escalate/bind/impersonate, no aggregation; `sre-agent-verify` checks other bindings |
| Endpoint auth | Webhook: Bearer token (from a Secret, shared with Alertmanager via a mounted file). Dashboard/API: Basic auth, PBKDF2 hashes, approver/viewer roles, CSRF tokens on forms, rate limiting, CSP |
| Credentials | Gmail App Password, webhook token, approver hashes, TLS key: Kubernetes Secrets mounted as files (0400), never in env or logs |
| Network | NetworkPolicy: in from `monitoring` (8080) and the dashboard LB (8443, LB allows one IP); out only DNS, 443, 587 |
| Pod | PSA restricted, non-root, read-only root FS, no capabilities, seccomp RuntimeDefault |
| Multi-cluster | Each cluster has its own kubeconfig/token and client configuration; an alert must name exactly one configured cluster or it is refused |

## Configuration

Environment variables (ConfigMap `sre-agent-config`; secrets via `*_FILE`):

| Variable | Default | Meaning |
|---|---|---|
| `SRE_AGENT_CLUSTER_NAME`, `SRE_AGENT_CLUSTER_ENV` | local, dev | identity of the in-cluster cluster |
| `SRE_AGENT_CLUSTERS_FILE` | – | YAML list of clusters for multi-cluster (below) |
| `SRE_AGENT_SMTP_HOST/PORT/USERNAME/FROM`, `SRE_AGENT_SMTP_PASSWORD_FILE` | – | SMTP (Gmail: smtp.gmail.com:587 STARTTLS) |
| `SRE_AGENT_EMAIL_TO`, `SRE_AGENT_EMAIL_TO_<SEVERITY>`, `SRE_AGENT_EMAIL_TO_<ENV>_<SEVERITY>` | – | recipients; most specific wins |
| `SRE_AGENT_EMAIL_MIN_SEVERITY` | MEDIUM | no emails below this |
| `SRE_AGENT_EMAIL_DEDUP_MINUTES` | 60 | same workload + same diagnosis: one email per window |
| `SRE_AGENT_APPROVAL_TTL_HOURS` | 24 | Pending approvals expire after this |
| `SRE_AGENT_CRITICAL_NAMESPACES` | payments,core-banking | `warning` alerts there become HIGH |
| `SRE_AGENT_LOG_TAIL_LINES`, `SRE_AGENT_LOG_MAX_BYTES`, `SRE_AGENT_MAX_EVENTS`, `SRE_AGENT_MAX_PODS` | 200, 32000, 40, 5 | evidence limits |
| `SRE_AGENT_API_TIMEOUT_SECONDS`, `SRE_AGENT_INVESTIGATION_TIMEOUT_SECONDS`, `SRE_AGENT_MAX_CONCURRENT` | 10, 60, 3 | time and concurrency limits |
| `SRE_AGENT_LLM_PROVIDER` | none | none, anthropic, openai, bedrock |
| `SRE_AGENT_LLM_MODEL`, `SRE_AGENT_LLM_API_KEY_FILE`, `SRE_AGENT_LLM_REGION` | – | LLM settings |

Alert thresholds live in `platform/monitoring/k8s-incident-rules.yaml` (edit `expr`/`for`, re-run
`make sre-agent-up`). Severity: `severity` label (critical/warning/info) or an explicit `sre_severity`.

**Turning on an LLM later:** add the key to the Secret
(`kubectl -n sre-agent patch secret sre-agent-secrets --type merge -p '{"stringData":{"llm-api-key":"..."}}'`),
set `SRE_AGENT_LLM_PROVIDER` (and optionally `SRE_AGENT_LLM_MODEL`) in the ConfigMap, then
`kubectl -n sre-agent rollout restart deploy/sre-agent`. The rule report stays the fallback. Bedrock also
needs boto3 in `requirements.txt` and `bedrock:InvokeModel` on the node role (IRSA is blocked here).

**More clusters (UAT, prod):** in each cluster create the same ServiceAccount + ClusterRole
(`kubectl apply -f apps/sre-agent/deploy/rbac.yaml` there), mint a token
(`kubectl -n sre-agent create token sre-agent --duration=720h`), build a kubeconfig with it, store it as a
Secret mounted into the agent, and list the clusters in `SRE_AGENT_CLUSTERS_FILE`:
```yaml
clusters:
  - {name: bank-sim, environment: dev, in_cluster: true}
  - {name: uat-eks, environment: uat, kubeconfig: /etc/sre-agent/clusters/uat.yaml}
```
Point each cluster's Alertmanager at `...?cluster=<its name>`. Incidents, history and email routing are per cluster.

## Agent observability

`/metrics`: `sre_agent_alerts_received_total`, `..._alerts_processed_total`, `..._investigation_duration_seconds`,
`..._ai_analysis_duration_seconds`, `..._ai_failures_total`, `..._email_delivery_failures_total`,
`..._incidents_total{severity}`, `..._pending_approvals`, `..._kubernetes_api_errors_total`,
`..._investigation_timeouts_total`. A ServiceMonitor scrapes it; alerts `SreAgentDown`, `SreAgentEmailFailing`,
`SreAgentKubernetesApiErrors`, `SreAgentInvestigationTimeouts`. Logs are JSON on stdout.

## Tests

`make sre-agent-test` (also in the GitLab pipeline as `sre-agent-test`): 44 tests, no cluster needed.
- Scenarios against a fake cluster: CrashLoopBackOff, OOMKilled, ImagePullBackOff, Pending (CPU, taint),
  Node NotReady, insufficient evidence, missing objects, CreateContainerConfigError, PVC, CoreDNS, RBAC 403s.
- Security: allowlist, static scan for write/exec/Secret calls, RBAC manifest, redaction, approval makes zero API calls.
- API: webhook auth, unknown/ambiguous cluster refused, correlation, email dedup and failure retry, login,
  viewer can't approve, rejection reason, expiry, CSRF, filters, metrics, rate limit.
- Integration: the real Kubernetes client against a stub API server, proving only GET requests with the right
  cluster's token.

## Troubleshooting

| Symptom | Check |
|---|---|
| No incidents | Alertmanager → Status shows the `sre-agent` receiver? `kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-alertmanager 9093`, Alerts page; agent logs `kubectl -n sre-agent logs deploy/sre-agent` |
| Webhook 401 in Alertmanager logs | token mismatch: re-run `make sre-agent-up` (it re-copies the token) |
| Email `failed` | Gmail needs an **App Password** (2-step verification on), not your normal password; `SreAgentEmailFailing` alert; the dashboard shows the error; it retries 5 times |
| Evidence gaps `403` | something the investigation needs isn't in the ClusterRole: add a **read** verb only |
| Image pull error for the agent | the node role has ECR read; check the image tag printed by the install |
| Dashboard unreachable | laptop IP changed: re-run with the new `DASHBOARD_IP`, or port-forward |

## Not verified from the build environment

The code, tests and manifests were verified offline. The image build/push to ECR, the live RBAC checks,
Alertmanager delivery and Gmail delivery were **not** run from here (no access to your cluster or Gmail):
`make sre-agent-up` and `make sre-agent-verify ARGS=--incident` on the jump host are those checks.

Remove: `make sre-agent-down` (`DELETE_IMAGES=1` also deletes the ECR repository). `make tf-down` removes
the dashboard load balancer and the agent's volume with everything else.
