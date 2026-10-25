1. `kubectl -n treasury-markets get pods -w` - watch the STATUS column go Running -> OOMKilled -> CrashLoopBackOff.
2. `kubectl -n treasury-markets describe pod <pod>`: look at **Last State**, **Reason** and **Exit Code** (137 = SIGKILL).
3. Compare the pod's memory limit with what the app does: `kubectl -n treasury-markets logs <pod> --previous | tail`.
4. What changed? `kubectl -n treasury-markets rollout history deploy/risk-calc-engine`
5. The fastest safe restoration for a bad release is usually not a fix-forward.
