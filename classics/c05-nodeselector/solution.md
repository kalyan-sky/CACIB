# c05 - Pending: node selector matches no node

```bash
kubectl -n c05-nodeselector describe pod -l app=analytics | tail -3
#   0/1 nodes are available: 1 node(s) didn't match Pod's node affinity/selector.
kubectl -n c05-nodeselector patch deploy analytics --type json   -p '[{"op":"remove","path":"/spec/template/spec/nodeSelector"}]'
# alternative (if the hardware really matters): kubectl label node <node> disktype=ssd
```

**Takeaways**
- `nodeSelector` / required node affinity are hard constraints: no matching node, no scheduling.
- Labelling a node is a platform decision (it attracts other workloads too) - prefer fixing the
  workload unless the constraint is real.
- Preferred (soft) affinity expresses "nice to have" without blocking scheduling.
