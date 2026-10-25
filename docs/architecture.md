# Architecture: simulation vs. a real bank private cloud

The lab keeps the *shape* of a regulated private-cloud container platform and
swaps each heavy component for something that runs on a laptop.

| Layer | In a bank's private cloud (typical) | In this simulation |
|---|---|---|
| Infrastructure | VMware vSphere / OpenStack / bare metal in 2+ data centres | Docker containers acting as nodes |
| Kubernetes distribution | OpenShift, Rancher (RKE2), Tanzu, or upstream kubeadm | kind (kubeadm-based) |
| Topology | Zones / racks / DCs, stretched or one cluster per DC | 3 workers labelled `sg-dc1-a`, `sg-dc1-b`, `sg-dc2-a` |
| Networking | Calico, Cilium, OVN-Kubernetes. Firewalls between zones | kindnet (or Calico) + NetworkPolicies |
| Ingress | F5 / NGINX / HAProxy routers, WAF, mTLS | `traffic-simulator` namespace as the only allowed entry to edge apps |
| Registry | Harbor / Artifactory / Quay, scanned, mirrored, air-gapped | Image loaded straight into the nodes (`kind load`) |
| Tenancy | Namespace per app or domain, quotas, PSA/SCC, RBAC from AD groups | Namespace per business domain, quota, PSA `restricted`, team groups |
| Delivery | Helm/Kustomize templates, Argo CD / Flux, Jenkins / GitLab CI | `tools/platform.py render` + `kubectl apply` |
| Observability | Prometheus, Grafana, ELK/Splunk, synthetic monitoring | JSON logs, `/metrics`, the traffic simulator's SLO view |
| Governance | MAS TRM, change management (ServiceNow), internal audit | Tickets in `scenarios/`, `platform.py audit` |

## Namespaces

| Namespace | Domain | Examples |
|---|---|---|
| `payments` | Payments & cash management | payments-gateway, paynow-fast-adapter, swift-connector, giro-*, meps-* |
| `core-banking` | Accounts, ledgers, loans | core-ledger, account-service, eod-batch-*, interest-accrual-* |
| `trade-finance` | LCs, guarantees, SCF | lc-issuance, bank-guarantee-*, swift-mt7xx-* |
| `treasury-markets` | FX, rates, liquidity, market risk | fx-rates, risk-calc-engine, alm-*, lcr-nsfr-* |
| `risk-compliance` | Fraud, sanctions, AML/KYC, credit | fraud-screening, sanctions-screening, kyc-onboarding |
| `channels` | Corporate portal, H2H, API banking | corporate-portal-api, host-to-host-*, open-api-gateway-* |
| `data-reporting` | MAS and management reporting | mas-610-reporting, regulatory-datamart-*, xbrl-generator-* |
| `shared-services` | Identity, notifications, audit | auth-token-service, audit-log-service, notification-service |
| `sim-traffic` | (platform) synthetic customers | traffic-simulator |

## Tiers

| Tier | Meaning | Platform rules |
|---|---|---|
| 1 | Critical: customer- or regulator-facing, tight RTO | 2+ replicas, PDB `maxUnavailable: 1`, zone spread |
| 2 | Important: degraded service tolerated briefly | 1 replica, standard probes and limits |
| 3 | Best effort: batch, internal reporting | 1 replica |

## Request path and failure propagation

Every `txn-service` instance calls each of its `UPSTREAMS` on every
`/transaction` (2 s timeout). An upstream failure turns into a `502` that
propagates to the customer-facing entry point. The traffic simulator measures
the success rate per entry point. `python3 tools/platform.py inventory` ranks
services by **blast radius**: how many apps break transitively when one is down.

## Service knobs (environment variables)

| Variable | Effect |
|---|---|
| `UPSTREAMS` | comma-separated base URLs called per transaction |
| `REQUIRED_CONFIG` | keys that must be set, or the process exits 1 at start (CrashLoopBackOff) |
| `FAIL_RATE` | fraction of transactions that fail with 500 |
| `LATENCY_MS` | base latency per transaction (with jitter) |
| `LEAK_MB_PER_SEC` | allocate and hold memory every second (leads to OOMKilled) |
| `STARTUP_DELAY_MS` | time before `/readyz` returns 200 |

Use them to build your own scenarios, e.g. `kubectl -n treasury-markets set env deploy/fx-rates LATENCY_MS=1500`
and watch timeouts cascade through payments.
