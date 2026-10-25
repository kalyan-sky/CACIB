# INC0048213 - reference solution

## Triage
```bash
./sim.sh status                                    # payments-gateway 0/2, CrashLoopBackOff
kubectl -n payments get pods -l app.kubernetes.io/name=payments-gateway
kubectl -n payments logs deploy/payments-gateway --previous
# {"level":"FATAL","msg":"missing required configuration, refusing to start","key":"DB_URL",...}
kubectl -n payments describe pod -l app.kubernetes.io/name=payments-gateway | grep -A5 "Last State"
kubectl diff -f rendered/payments/payments-gateway.yaml   # shows DB_URL missing from the ConfigMap
```

## Restore service
```bash
kubectl apply -f rendered/payments/payments-gateway.yaml  # put the approved config back
kubectl -n payments rollout restart deploy/payments-gateway
kubectl -n payments rollout status deploy/payments-gateway
./sim.sh status                                          # payments-gateway HEALTHY again
```

## Why it happened (root cause for the RCA)
- The change edited the ConfigMap in place and accidentally removed `DB_URL`.
  Nothing failed at that moment: running pods had already read their environment.
- The latent fault surfaced only when the overnight maintenance recycled the pods.
  Every new pod read the broken ConfigMap and crashed. Both replicas failed
  together, so the PDB and the 2 replicas gave no protection.
- The change was not made through the pipeline, so `rendered/` (git) and the
  cluster had drifted apart without anyone noticing.

## Preventive actions (what a platform engineer proposes)
- **GitOps with self-heal** (Argo CD / Flux): manual drift is reverted within
  minutes and shows up as "OutOfSync".
- **RBAC**: application teams get no `update` on production ConfigMaps. Changes
  go through a PR to the inventory.
- **Immutable, versioned config**: a config hash on the pod template (this
  platform already renders `bank.sim/config-hash`), or name-suffixed ConfigMaps
  (kustomize `configMapGenerator`). A config change then becomes a *rollout*.
  With `maxUnavailable: 0`, a broken rollout stalls on the first new pod and
  the old pods keep serving.
- Validate required keys in CI before deployment.

## Talking points
CrashLoopBackOff · `logs --previous` · envFrom/ConfigMap lifecycle · config
drift · GitOps · MAS incident notification timelines · blameless RCA.
