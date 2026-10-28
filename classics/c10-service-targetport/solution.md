# c10 - wrong targetPort

```bash
kubectl -n c10-service-targetport exec deploy/client -- wget -qO- -T 3 http://catalog
#   wget: can't connect to remote host (10.x.x.x): Connection refused
kubectl -n c10-service-targetport get endpoints catalog          # 10.x.x.x:8080,...  <- nginx listens on 80
kubectl -n c10-service-targetport patch svc catalog --type json   -p '[{"op":"replace","path":"/spec/ports/0/targetPort","value":80}]'
```

**Takeaways**
- The error tells you the layer: `bad address` = DNS; `connection refused` = reached an IP, nothing
  listening on that port; `timeout` = packets dropped (NetworkPolicy, security group, routing).
- Name container ports (`name: http`) and use `targetPort: http` so the Service follows the container.
