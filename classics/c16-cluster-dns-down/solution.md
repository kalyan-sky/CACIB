# c16 - cluster DNS (CoreDNS) is down

```bash
kubectl -n c16-cluster-dns-down logs deploy/client --tail=2        # dns lookup FAILED
kubectl -n kube-system get deploy coredns                           # READY 0/0
kubectl -n kube-system get endpoints kube-dns                       # <none>
kubectl -n kube-system scale deploy coredns --replicas=2
kubectl -n kube-system rollout status deploy/coredns
kubectl -n c16-cluster-dns-down logs deploy/client -f               # dns OK
```

**Takeaways**
- "Everything is broken at once" usually means one **shared** dependency: DNS, the CNI, the API
  server, an expired certificate, a full disk. Find what all the failures have in common before
  debugging individual apps.
- Every pod's `/etc/resolv.conf` points at the `kube-dns` Service IP; with no CoreDNS endpoints,
  every lookup fails. Check `kube-system` early in a cluster-wide incident.
- On EKS, CoreDNS is a managed add-on: it may restore itself, and its version should follow the
  cluster version during upgrades.
