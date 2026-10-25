# Interview notes: turning the lab into answers

Use the lab to build **stories**, not just knowledge. After each ticket, write
a STAR summary (Situation, Task, Action, Result) of 4-5 lines. Interviewers for
platform roles in banks often probe for three things: you can run production
calmly, you know *why* the guardrails exist, and you can work inside change
and risk controls.

## The platform pitch (30 seconds)
"With 200+ applications and a small platform team, you can't hand-craft each
deployment. I'd build a golden path: one templated, compliant deployment
pattern driven from an inventory. Every app then gets probes, limits,
hardened security contexts, PDBs, zone spread, quotas and generated network
policies by default. Then I'd enforce it with policy-as-code and GitOps,
and measure it with a live control audit and customer-flow SLOs."

## Likely questions and where the lab covers them

| Question | Scenario / file |
|---|---|
| A pod is in CrashLoopBackOff. Walk me through it. | 01 |
| Difference between requests and limits? What happens at the limit? | 02, 05 |
| How do you do zero-downtime deployments? | 04, 06 (`maxUnavailable: 0`, readiness) |
| How do NetworkPolicies work? How would you roll out zero-trust? | 03, `allow-from-consumers` generation |
| How do you patch/upgrade nodes without downtime? | 07 (PDB, drain, zones) |
| A service is up but unreachable. What do you check? | 08 (endpoints, selectors) |
| How do you onboard a new app team? | 09 (golden path, RBAC, quotas) |
| How do you meet audit/regulatory requirements (e.g. MAS TRM) on Kubernetes? | 10, `platform.py audit` |
| How do you prevent config drift? | 01, 10 (GitOps, `apply` vs `replace`) |
| Capacity planning on-prem? | 05, `make capacity` |
| Multi-tenancy for many teams? | RBAC model in `platform/cluster-rbac.yaml`, quotas, PSA |

## Private-cloud specifics to be ready for
- **No managed control plane.** You own etcd backups, certificate rotation,
  control-plane upgrades, node OS patching and runtime CVEs.
- **Capacity is finite.** Hardware lead times are long, so right-sizing,
  quotas and showback by cost centre matter more than in public cloud.
- **Air-gapped or proxied networks.** Internal registry mirrors, an internal
  CA for TLS, and proxies for egress. Many "image pull" and "TLS" incidents
  come from here.
- **Two data centres.** Active-active vs active-passive, stretched clusters vs
  one cluster per DC, data replication for stateful services, and DR drills
  with evidence.
- **Regulated change.** CAB, standard vs normal vs emergency changes,
  segregation of duties, privileged access via PAM, and audit trails.
- **Integration with legacy.** Mainframe or core banking packages, MQ, SWIFT
  gateways and batch windows. Not everything is a stateless microservice.

## Questions to ask them
- Which distribution do you run (OpenShift, Rancher, Tanzu, vanilla)? One cluster per environment or per DC?
- How are the 200+ apps delivered today: a shared Helm library, GitOps, per-team pipelines?
- What is the split between platform team and app teams for on-call?
- Biggest pain point right now: upgrades, capacity, onboarding, or audit findings?
