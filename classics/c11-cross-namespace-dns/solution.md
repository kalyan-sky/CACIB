# c11 - cross-namespace Service DNS

```bash
kubectl -n c11-cross-namespace-dns logs deploy/frontend --tail=2
#   request to http://backend failed: wget: bad address 'backend'
kubectl -n c11-cross-namespace-dns-data get svc backend
kubectl -n c11-cross-namespace-dns set env deploy/frontend   BACKEND_URL=http://backend.c11-cross-namespace-dns-data.svc.cluster.local
kubectl -n c11-cross-namespace-dns logs deploy/frontend -f      # backend OK
```

**Takeaways**
- Pod DNS search path: `<own-ns>.svc.cluster.local`, `svc.cluster.local`, `cluster.local`.
  `backend` -> `backend.<own-ns>.svc.cluster.local` only. Across namespaces use
  `backend.<ns>` or the full name.
- `bad address` / `no such host` / `NXDOMAIN` = a DNS problem: check the name first, then CoreDNS (c16).
- Cross-namespace calls are also where NetworkPolicies usually bite next.
