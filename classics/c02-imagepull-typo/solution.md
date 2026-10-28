# c02 - ImagePullBackOff

```bash
kubectl -n c02-imagepull-typo describe pod -l app=api | tail
#   Failed to pull image "nginx:1.27-alpien": ... not found
kubectl -n c02-imagepull-typo set image deploy/api api=nginx:1.27-alpine
kubectl -n c02-imagepull-typo rollout status deploy/api
```

**Takeaways**
- The event text tells you *which* pull problem it is: `not found` / `manifest unknown`
  (wrong name or tag), `unauthorized` / `denied` (registry credentials, imagePullSecrets),
  `i/o timeout` / `TLS` (network, proxy, private CA - common in bank private clouds).
- `ErrImagePull` becomes `ImagePullBackOff` while kubelet waits between retries.
- Prevent it: pin images by digest, check the tag exists in CI, admission policy for allowed registries.
