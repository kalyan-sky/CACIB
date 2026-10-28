1. `kubectl -n {{NS}} get pods` - look at READY, not STATUS.
2. `kubectl -n {{NS}} get endpoints shop` - a Service only sends traffic to **Ready** pods.
3. `kubectl -n {{NS}} describe pod -l app=shop | grep -i readiness` and the events below it.
4. Does nginx have a `/healthz` page? `kubectl -n {{NS}} exec deploy/client -- wget -S -O- http://<pod-ip>/healthz`
