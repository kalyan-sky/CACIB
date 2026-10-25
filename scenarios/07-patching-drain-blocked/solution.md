# CHG0032011 - reference solution

```bash
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
#   error when evicting pods/"core-ledger-..." -n "core-banking" (will retry after 5s):
#   Cannot evict pod as it would violate the pod's disruption budget.
kubectl get pdb -A | awk 'NR==1 || $5==0'    # ALLOWED DISRUPTIONS = 0
kubectl -n core-banking get pdb core-ledger -o jsonpath='{.spec}'   # {"minAvailable":2,...} with 2 replicas

# Fix the PDB to the platform standard (or: kubectl apply -f rendered/core-banking/core-ledger.yaml)
kubectl -n core-banking patch pdb core-ledger --type=merge -p '{"spec":{"minAvailable":null,"maxUnavailable":1}}'
kubectl get pdb -n core-banking core-ledger     # ALLOWED DISRUPTIONS 1

kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
kubectl label node <node> bank.sim/os-patch-level=2026-10 --overwrite
kubectl uncordon <node>
kubectl get nodes -L topology.kubernetes.io/zone,bank.sim/os-patch-level
```

## What went wrong
`minAvailable: 2` with `replicas: 2` means **zero** allowed disruptions, so
the Eviction API refuses forever. A PDB guards against *voluntary* disruption
(drain, cluster upgrade). It does not protect against node crashes, so it must
leave room for maintenance. `maxUnavailable: 1` scales with the replica count.

## Alternative: scale up first
`kubectl -n core-banking scale deploy core-ledger --replicas=3` makes one
disruption possible without touching the PDB. Remember to scale back. Check
that a third pod fits the quota and the nodes, and that it does not drift from
git (GitOps would revert it).

## Private-cloud angle
- No managed node pools: the platform team owns OS patching, kernel CVEs,
  container-runtime and Kubernetes upgrades, node by node, zone by zone.
- Check capacity **before** draining. `kubectl describe node | grep -A5 Allocated`:
  can the remaining nodes in other zones absorb the evicted pods?
- Patch one zone at a time. Tier-1 pods are spread across zones by
  `topologySpreadConstraints`, so a whole zone can be out without an outage.
- Automate it: kured (reboot daemon), or a pipeline that drains, patches,
  verifies and uncordons, with evidence for the change record.
