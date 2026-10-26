# INC0048402 - reference solution

```bash
kubectl -n trade-finance get pods -l app.kubernetes.io/name=lc-issuance
#   lc-issuance-7d9..   1/1 Running            <- old ReplicaSet, still serving
#   lc-issuance-5f4..   0/1 ImagePullBackOff   <- new ReplicaSet
kubectl -n trade-finance describe pod <new-pod> | tail -5
#   Failed to pull image ".../txn-service:1.5.1-hotfx": ... not found
kubectl -n trade-finance rollout history deploy/lc-issuance
kubectl -n trade-finance rollout undo deploy/lc-issuance
kubectl -n trade-finance rollout status deploy/lc-issuance
```

**Customer impact: none.** The platform's deployment strategy is
`maxSurge: 1, maxUnavailable: 0`. Kubernetes starts a new pod first and only
removes an old one after the new one is Ready. The new pod never became Ready,
so the old ReplicaSet kept serving. This is the reason the golden path sets
those values.

**Root cause:** typo in the image tag (`1.5.1-hotfx`). The image does not
exist in the registry.

## Private-cloud angle
- In a bank's private cloud, nodes usually cannot reach Docker Hub. Images come
  from an internal registry (Harbor, Artifactory, Quay, Nexus) that mirrors
  and scans approved images. Pull errors are often registry auth (imagePullSecrets),
  a mirror lag, a proxy, or certificates signed by the bank's internal CA.
- Prevent it: the pipeline verifies the tag/digest exists (`crane digest`,
  `skopeo inspect`) before deploying. Pin by **digest**. Enable tag
  immutability in the registry. An admission policy only allows the internal
  registry (the audit's control C04).
- `progressDeadlineSeconds` marks the rollout as failed (`ProgressDeadlineExceeded`), so pipelines and alerts can react.
