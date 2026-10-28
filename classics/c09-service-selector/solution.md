# c09 - Service selector does not match the pods

```bash
kubectl -n c09-service-selector get endpoints orders          # <none>
kubectl -n c09-service-selector get svc orders -o wide        # SELECTOR app=order
kubectl -n c09-service-selector get pods --show-labels        # app=orders
kubectl -n c09-service-selector patch svc orders -p '{"spec":{"selector":{"app":"orders"}}}'
kubectl -n c09-service-selector get endpoints orders          # 2 addresses
```

**Takeaways**
- A Service finds pods only by label selector -> EndpointSlices. One wrong character = zero endpoints,
  while every pod looks healthy.
- First question for "Service unreachable": *does it have endpoints?* No -> selector or readiness.
  Yes -> ports, NetworkPolicy, or the app itself (see c10).
