# c08 - Running but not Ready: readiness probe on a missing path

```bash
kubectl -n c08-readiness-path get pods                  # shop 0/1 Running
kubectl -n c08-readiness-path get endpoints shop        # <none>
kubectl -n c08-readiness-path describe pod -l app=shop | grep Readiness
#   Readiness probe failed: HTTP probe failed with statuscode: 404
kubectl -n c08-readiness-path patch deploy shop --type json   -p '[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/"}]'
kubectl -n c08-readiness-path exec deploy/client -- wget -qO- -T 3 http://shop | head -3
```

**Takeaways**
- **Readiness** failing = pod stays Running but is removed from Service endpoints (no traffic).
  **Liveness** failing = the container is restarted. **Startup** probe = holds the other two
  off until the app has started.
- `Running` is not `Ready`, and `Ready` is not "the business flow works" - check endpoints.
- Probe a cheap, dependency-free endpoint for liveness; readiness may check critical dependencies.
