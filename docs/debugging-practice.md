# Practising debugging with the bank simulation

The goal is to get **fast and calm** at the loop every on-call engineer runs:

```
1 IMPACT     who is affected, since when?            portal, ./sim.sh status, Argo CD health
2 CHANGE     what changed recently?                  Argo CD history/diff, git log gitops, rollout history, events
3 HYPOTHESIS one sentence: "X fails because Y"
4 EVIDENCE   prove or kill it with one command       describe, logs --previous, endpoints, probe, top
5 MITIGATE   restore service first                   rollout undo, git revert, scale, uncordon
6 FIX        root cause, through Git                 merge request → pipeline → Argo CD
7 VERIFY     the check passes, customers OK          ./sim.sh check, portal, make audit
8 WRITE UP   timeline, root cause, prevention        docs/triage-cheatsheet.md template
```
Say each step out loud (or write it) while practising. Interviewers listen for the order.
**Mitigate before you perfect**: a `rollout undo` in minute 3 beats a perfect fix in minute 40.

## Your toolbox, by layer

| Layer | Look with | Typical findings |
|---|---|---|
| Customer | the portal (trace button), `./sim.sh status` | which business flow, which hop failed |
| Delivery | Argo CD UI: **App Diff**, **History and Rollback**; `git log gitops`; GitLab pipeline | what changed, who merged it, when |
| Workload | `kubectl get/describe`, `logs --previous`, `rollout history`, `get events --sort-by=.lastTimestamp` | CrashLoopBackOff, OOMKilled, probes, image pulls |
| Service / network | `get endpoints`, `get svc -o wide`, `get netpol`, `kubectl exec deploy/<app> -- /txn-service probe <url>` | empty endpoints, wrong selector, blocked traffic (timeout) |
| Resources | `kubectl top pods/nodes` (metrics-server), `describe resourcequota`, `describe node` | limits, quota, capacity |
| Node | `kubectl debug node/<node> -it --image=busybox:1.36`, or SSM into the EC2 instance | kubelet, disk, DNS on the node |
| AWS | `aws eks describe-nodegroup … --query nodegroup.health`, Auto Scaling activities, CloudTrail | instance launch failures, denied API calls |

**Debugging inside a scratch image.** The app containers have no shell. Use the built-in client:
```bash
kubectl -n payments exec deploy/payments-gateway -- /txn-service probe http://fraud-screening.risk-compliance.svc.cluster.local:8080/healthz
```
Or attach an ephemeral debug container that respects Pod Security `restricted`:
```bash
kubectl -n payments debug -it deploy/payments-gateway --image=busybox:1.36 --target=app --profile=restricted -- sh
#   nslookup fraud-screening.risk-compliance.svc.cluster.local ; wget -qO- http://127.0.0.1:8080/metrics
```
(Without `--profile=restricted` the debug container is rejected. That's Pod Security working, and a good thing to explain.)

## Level 0: classic Kubernetes cases (any cluster, free)

Start here if a symptom like `CreateContainerConfigError` or `Init:0/1` isn't instantly familiar:
`./k8s-classics.sh list`. There are 16 single-cause cases; see [`../classics/README.md`](../classics/README.md).

## Level 1: the ten tickets (repeat until each takes < 10 minutes)

```bash
./sim.sh list
./sim.sh page            # random incident: you don't know which one is coming
./sim.sh check <id>      # records time-to-resolve
./sim.sh solution <id>   # compare your path with the reference
./sim.sh reset <id>
```
Rules for yourself:
1. No `hint` for the first 5 minutes.
2. Before typing a fix, write the one-sentence hypothesis.
3. Each time, also find the incident in the **portal** and in **Argo CD** (OutOfSync + diff).
   That is how you'd learn about it in real life.

Target times: 01, 04, 06, 08 under 5 min · 02, 03, 05 under 10 min · 07, 10 under 15 min.

## Level 2: GitOps failure drills (no script, you break it through Git)

Each drill: break it on a branch → merge → watch → debug → recover **through Git**.

| # | Break it like this | What you should find | Recover |
|---|---|---|---|
| G1 | In the inventory, give `fraud-screening` `config: {LATENCY_MS: "3000"}` | Payments time out (the upstream timeout is 2 s); the trace points at fraud-screening; Argo CD history shows the commit | `git revert` on main, or Revert on `gitops` |
| G2 | Add an upstream that doesn't exist (`payments/no-such-app`) | **The pipeline goes red** in `manifests`: the render refuses unknown upstreams. Nothing reached the cluster | fix the inventory; note the value of tests in the MR |
| G3 | Commit directly to the `gitops` branch: an image tag that doesn't exist on one deployment | ImagePullBackOff, rollout stuck, old pods still serving | Revert the commit on `gitops`; then explain why direct commits to `gitops` are forbidden |
| G4 | Commit directly to `gitops`: add a `kind: Secret` manifest | Argo CD sync fails: *not permitted in project bank-sim* | Revert; explain the AppProject guardrail |
| G5 | `kubectl -n payments scale deploy payments-gateway --replicas=0` | Portal: MEPS/SWIFT payments fail; Argo CD: **OutOfSync**, diff shows `replicas` | **Sync** in Argo CD; then enable `selfHeal` and repeat: Argo CD reverts it within seconds |
| G6 | Raise `kubernetes_version` in `terraform.tfvars` by one minor version (only if EKS offers it) | `terraform plan`: control plane in-place update, then the node group | Run it while watching `./sim.sh status`: zero-downtime upgrade |

## Level 3: infrastructure failures (AWS side)

| # | Do this | Practise |
|---|---|---|
| A1 | EC2 console → **stop** one worker instance (simulates losing an AZ) | Node `NotReady`; pods reschedule after ~5 min; tier-1 apps stay up (2 replicas, zone spread); the Auto Scaling group replaces the instance |
| A2 | `kubectl cordon` + `drain` a node while ticket 07's PDB is broken | PDB blocking evictions; drain vs. node group upgrade |
| A3 | `kubectl -n kube-system scale deploy coredns --replicas=0` | Everything fails with DNS errors (`no such host` in traces): a classic "everything is broken" incident with one root cause. Scale back to 2, unless the EKS add-on manager restores it first (it owns that deployment); notice which happened |
| A4 | Set `FAIL_RATE=0.2` on `core-ledger` (`kubectl set env`) | Partial failures across *every* flow; use the blast-radius ranking (`make inventory`) to reason from symptoms to the shared dependency |

Undo A3/A4 with `./sim.sh reset` style restores, or by syncing in Argo CD.

## Make it realistic

- **Pair up:** a friend runs `./sim.sh start <id>` (or a G/A drill) without telling you which. You get
  only the ticket text.
- **Time-box and write the note:** 4–5 lines in STAR form after every drill. These become
  your interview stories ("the time an image tag typo stalled a release, and why
  customers weren't affected").
- **Explain the "why" of each guardrail you meet:** `maxUnavailable: 0`, PDBs, NetworkPolicies,
  quotas, Pod Security, the AppProject, `selfHeal`. Each one is an interview question.
