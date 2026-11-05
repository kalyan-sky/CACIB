# Monitoring: Prometheus + Grafana, deployed by Argo CD

`make monitoring` adds the platform's observability stack to the EKS cluster the GitOps way:
Argo CD installs **kube-prometheus-stack** from its Helm chart as a second application,
`monitoring`, next to `bank-sim`.

```
 Argo CD ── app bank-sim   (gitops branch, GitLab)  ──► 8 bank namespaces, ~70 pods  ──┐ /metrics :8080
         └─ app monitoring (Helm chart, pinned)     ──► Prometheus ◄──────────────────┘
                                                         │  + kubelet/cAdvisor, kube-state-metrics, node-exporter
                                                         ├─► Alertmanager (bank + Kubernetes alerts)
                                                         └─► Grafana ── load balancer, your IP only
```

Needs `compute = "ec2"`. node-exporter is a DaemonSet, and the load balancer needs EC2 targets.

## Run it (on the jump host)

```bash
cd ~/CACIB/terraform && terraform apply                   # compute = "ec2"
cd ~/CACIB
REPO_URL=https://gitlab.com/kalyansiddam0/cacib.git ARGOCD_UI_IP=<laptop IP> make gitops-bootstrap
make monitoring UI_IP=<laptop IP>                          # 3-6 min; prints the Grafana URL and password
```
`<laptop IP>` is the public IP of the machine running your **browser**. Get it from
https://checkip.amazonaws.com on the laptop, not on the jump host. The Kubernetes API stays
reachable only from the jump host; only the two web UIs are opened to your laptop.

## What you get

| Where | What to look at |
|---|---|
| Grafana → **Bank - transactions overview** | Transactions/s, failure rate and latency per domain; the top failing and slowest apps; which **upstream** fails; restarts; versions running |
| Grafana → **Kubernetes / Compute Resources / Namespace (Pods)** | CPU and memory per pod against requests and limits (scenario 02 OOM, quotas) |
| Grafana → **Node Exporter / Nodes** | Per-node CPU, memory and disk (draining, A2 stopped instance) |
| Grafana → **Alerting → Alert rules** | `BankAppHighFailureRate`, `BankAppSlow`, `BankAppDown` plus ~100 Kubernetes alerts (`KubePodCrashLooping`, `KubeDeploymentReplicasMismatch`, …) |
| Argo CD → **monitoring** | The whole stack as a tree; it's **selfHeal on**, unlike `bank-sim` |

## Practise with it

Start an incident, and find it **first in Grafana**, before using kubectl:

| Ticket | What Grafana shows |
|---|---|
| `./sim.sh start 01` | Restarts jump in `payments`; *Pods not answering* > 0; payments failure rate rises |
| `./sim.sh start 02` | Compute Resources: `risk-calc-engine` memory climbs to its limit, then a restart |
| `./sim.sh start 03` | Payments failure rate and the *upstream errors* panel point at `fraud-screening`. The NetworkPolicy blocks it, so the pod looks healthy |
| `./sim.sh start 08` | `corporate-portal-api` failures; upstream errors name `account-service` |
| G1 drill (`LATENCY_MS: "3000"` on fraud-screening) | *Slowest apps* shows fraud-screening; payments time out after 2 s |

Then write the incident timeline from the graphs. Interviewers like "we saw the failure rate
cross 5% at 10:42, and the upstream panel pointed at X".

## Design choices (interview answers)

- **Argo CD deploys the monitoring stack too,** so it's versioned (`targetRevision` pinned
  to a chart version), reviewed and self-healing. A separate AppProject (`platform`) may
  create cluster-wide objects; the bank's project may not.
- **Discovery by annotation:** every rendered pod carries `prometheus.io/scrape: "true"`.
  One scrape job finds all 216 apps without a ServiceMonitor per app.
- **Zero trust still applies:** the bank namespaces deny ingress by default, so the golden
  path renders `allow-from-monitoring` (TCP 8080 from namespace `monitoring` only).
- **Recording rules** (`bank:txn_failures:ratio5m`) keep dashboards and alerts cheap and consistent.
- **Not production-grade:** Prometheus uses an emptyDir with 12h retention, and Grafana is
  plain HTTP on an IP-restricted load balancer. In a bank: persistent storage or remote
  write (Amazon Managed Prometheus, Thanos), HTTPS behind SSO, and Alertmanager routing
  to PagerDuty or ServiceNow.

## Clean up

`YES=1 make tf-down` stops Argo CD first, so it can't recreate anything, then deletes the
Grafana and Argo CD load balancers before `terraform destroy`.
