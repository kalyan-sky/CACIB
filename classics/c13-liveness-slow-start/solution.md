# c13 - liveness probe kills a slow-starting app

```bash
kubectl -n c13-liveness-slow-start describe pod -l app=ledger | grep -E "Liveness probe failed|Killing"
#   Liveness probe failed: dial tcp ...:8080: connect: connection refused
#   Container ledger failed liveness probe, will be restarted
kubectl -n c13-liveness-slow-start patch deploy ledger --type json -p '[{"op":"add",
  "path":"/spec/template/spec/containers/0/startupProbe",
  "value":{"tcpSocket":{"port":8080},"periodSeconds":3,"failureThreshold":20}}]'
kubectl -n c13-liveness-slow-start get pods -w      # Ready after ~25 s, RESTARTS 0
```

**Takeaways**
- Liveness gives up after `initialDelaySeconds + periodSeconds * failureThreshold` (here ~8 s) -
  shorter than the 25 s start-up, so it restarts forever: a self-inflicted CrashLoop.
- `startupProbe` disables liveness/readiness until the app has started once (here up to 60 s),
  then liveness keeps guarding against hangs. Better than a huge `initialDelaySeconds`, which also
  delays detecting real failures.
- Common with JVMs, big caches and database migrations at start-up.
