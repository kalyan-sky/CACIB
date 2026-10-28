# c06 - Pending: untolerated taint

```bash
kubectl -n c06-taint-toleration describe pod -l app=payments-worker | tail -3
#   0/2 nodes are available: 1 node(s) had untolerated taint {dedicated: payments}, ...
kubectl -n c06-taint-toleration patch deploy payments-worker --type merge -p '{"spec":{"template":{"spec":
  {"tolerations":[{"key":"dedicated","operator":"Equal","value":"payments","effect":"NoSchedule"}]}}}}'
kubectl -n c06-taint-toleration get pod -o wide        # NODE column = the dedicated node
./k8s-classics.sh reset c06                             # removes the taint and label again
```

**Takeaways**
- **Taints** keep pods *off* a node; **tolerations** allow a pod *onto* it; **nodeSelector/affinity**
  pull a pod *towards* a node. Dedicated nodes use both: taint + label on the node,
  toleration + selector on the pod.
- Control-plane nodes carry `node-role.kubernetes.io/control-plane:NoSchedule` - the same mechanism.
- `NoExecute` also evicts running pods that don't tolerate it (used for node problems).
