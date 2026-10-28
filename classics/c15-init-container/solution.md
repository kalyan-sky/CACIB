# c15 - init container waits for the wrong Service name

```bash
NS=c15-init-container
kubectl -n $NS get pods                                   # payments-api ... Init:0/1
kubectl -n $NS logs deploy/payments-api -c wait-for-db --tail=3
#   ** server can't find db.c15-init-container.svc.cluster.local: NXDOMAIN
#   waiting for db...
kubectl -n $NS get svc                                    # the Service is called "database"
kubectl -n $NS edit deploy payments-api                   # db.$(POD_NAMESPACE) -> database.$(POD_NAMESPACE)
```

**Takeaways**
- Init containers run **in order, to completion**, before the app containers. `Init:N/M` shows progress;
  `Init:CrashLoopBackOff` means an init container is failing.
- `kubectl logs <pod> -c <init-container>` - always name the container.
- "Wait for dependency" init containers hide outages at start-up; many teams prefer the app to retry
  and use readiness instead.
