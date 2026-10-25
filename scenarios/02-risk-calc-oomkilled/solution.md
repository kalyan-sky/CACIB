# INC0048290 - reference solution

## Evidence
```bash
kubectl -n treasury-markets get pods -l app.kubernetes.io/name=risk-calc-engine
kubectl -n treasury-markets describe pod -l app.kubernetes.io/name=risk-calc-engine | grep -A4 "Last State"
#   Last State: Terminated  Reason: OOMKilled  Exit Code: 137
kubectl -n treasury-markets logs deploy/risk-calc-engine --previous | tail -3
#   "cache size" mb: 52, 56, 60 ... grows every second until the 64Mi limit
kubectl -n treasury-markets rollout history deploy/risk-calc-engine
#   REVISION 2  release v2.3.1: enable eager VaR cache
```
The nodes are fine. The kernel's OOM killer acted at the **container's cgroup
limit**, not at node memory pressure. `kubectl describe node` would show
`MemoryPressure=False`.

## Restore
```bash
kubectl -n treasury-markets rollout undo deploy/risk-calc-engine
kubectl -n treasury-markets rollout status deploy/risk-calc-engine
```

## Why not just raise the limit?
The new release has an **unbounded** cache, so memory grows linearly forever.
A bigger limit only buys minutes and lets one pod take node memory from other
tenants. Raise a limit when the app's *steady-state* need has grown (measured,
for example with `kubectl top` or Prometheus `container_memory_working_set_bytes`),
not to hide a leak. Hand the fix-forward back to the treasury team with the evidence.

## Why the pipeline said "success"
The pod passed readiness within 1 s and the rollout completed. It was
OOM-killed 15 s later. Only post-deployment verification catches this:
canary analysis, or watching restarts and error rates for N minutes before
marking a release good.

## Talking points
requests vs limits · QoS classes (Guaranteed/Burstable/BestEffort) · OOMKilled
vs eviction · exit code 137 · rollout history / undo · JVM heap vs container
limit (`-XX:MaxRAMPercentage`) · progressive delivery (Argo Rollouts).
