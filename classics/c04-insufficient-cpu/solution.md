# c04 - Pending: Insufficient cpu

```bash
kubectl -n c04-insufficient-cpu describe pod -l app=reports | tail -3
#   0/3 nodes are available: 3 Insufficient cpu.
kubectl describe node | grep -E "^Name:|cpu:" | head
kubectl -n c04-insufficient-cpu set resources deploy/reports --requests=cpu=100m --limits=cpu=500m
```

**Takeaways**
- The scheduler places pods by **requests**, not by real usage. A request larger than any
  node's *allocatable* can never be scheduled - no autoscaler can help either.
- Read the FailedScheduling message: `Insufficient cpu/memory`, `didn't match node selector`,
  `untolerated taint`, `volume node affinity conflict` each point to a different fix.
- Right-size from measurements (p95 usage), and let quotas/LimitRanges cap the extremes.
