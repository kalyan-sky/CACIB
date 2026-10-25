# INC0048477 - reference solution

```bash
kubectl -n shared-services get pods -l app.kubernetes.io/name=notification-service   # Running, 0/1 READY
kubectl -n shared-services get endpoints notification-service                         # <none>
kubectl -n shared-services describe pod -l app.kubernetes.io/name=notification-service | grep -i readiness
#   Readiness: http-get http://:8081/readyz ...
#   Warning  Unhealthy  Readiness probe failed: ... connect: connection refused
kubectl -n shared-services rollout history deploy/notification-service
kubectl -n shared-services rollout undo deploy/notification-service      # back to port http(8080) + RollingUpdate
# or: kubectl apply -f rendered/shared-services/notification-service.yaml
kubectl -n shared-services get endpoints notification-service
```

## Why a probe change caused an outage
- A pod that fails **readiness** keeps running but is removed from the
  Service's endpoints, so callers get "connection refused" / 503.
  (A failing **liveness** probe would restart it instead.)
- The change also switched the strategy to `Recreate`. Kubernetes killed every
  old pod *before* starting the new ones. With the platform default
  (`RollingUpdate`, `maxUnavailable: 0`) the new pod would never have become
  Ready, the rollout would have stalled, and the old pods would have kept serving.

## Lessons
- Running != Ready != serving. Monitor endpoint counts and synthetic transactions, not just pod phase.
- Use `startupProbe` for slow starters instead of long `initialDelaySeconds`.
- An admission policy (Kyverno/Gatekeeper) can deny `Recreate` for tier-1/2 services.
