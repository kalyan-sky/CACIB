# RITM0072210 - reference solution

```bash
kubectl -n trade-finance get deploy trade-docs-ocr          # 0/2, no pods
kubectl -n trade-finance describe rs -l app.kubernetes.io/name=trade-docs-ocr | tail -3
#   Error creating: pods "trade-docs-ocr-..." is forbidden: exceeded quota: compute-quota,
#   requested: requests.cpu=750m,requests.memory=768Mi, used: ..., limited: ...
kubectl -n trade-finance describe resourcequota compute-quota

kubectl -n trade-finance set resources deploy/trade-docs-ocr \
  --requests=cpu=100m,memory=128Mi --limits=cpu=250m,memory=256Mi
kubectl -n trade-finance rollout status deploy/trade-docs-ocr
python3 tools/platform.py capacity --live
```

## Why the quota check happens before scheduling
ResourceQuota is an **admission** check. The ReplicaSet controller's pod
creation is rejected by the API server, so no Pending pod ever exists. You only
see the reason in the ReplicaSet's events. Compare a pod that is admitted but
does not fit on any node: it stays `Pending` with a `FailedScheduling` event.

## The platform-team conversation
- 2 x 750m requested for an 80m workload means ~90% of the reserved CPU would
  never be used. Across 200+ apps, over-requesting is how a private cloud runs
  out of capacity while nodes sit idle. On-prem capacity takes months to buy;
  it is not an autoscaling group.
- 100m / 128Mi gives ~1.2x headroom over p95. A limit at 2x allows bursting.
  With requests below limits the pod is in QoS class `Burstable`.
- Quotas protect tenants from each other (the noisy-neighbour problem) and make
  showback/chargeback by cost centre possible.
- Golden path: add the app to the inventory with `resources: {cpu: 100m, memory: 128Mi}`
  and render it, so it gets the probes, labels, NetworkPolicy and PDB rules too.
