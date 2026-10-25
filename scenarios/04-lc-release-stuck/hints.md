1. `kubectl -n trade-finance rollout status deploy/lc-issuance` and `kubectl -n trade-finance get pods,rs -l app.kubernetes.io/name=lc-issuance`
2. Why are two ReplicaSets alive? Which one is serving traffic?
3. `kubectl -n trade-finance describe pod <new-pod>` - read the Events at the bottom.
4. Look closely at the image tag in the new ReplicaSet.
5. `kubectl rollout history` / `kubectl rollout undo`
