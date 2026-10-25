# INC0048530 - reference solution

```bash
kubectl -n core-banking get endpoints account-service        # ENDPOINTS <none>
kubectl -n core-banking get svc account-service -o wide      # SELECTOR app.kubernetes.io/name=account-svc
kubectl -n core-banking get pods -l app.kubernetes.io/name=account-service   # pods exist with the old label
kubectl apply -f rendered/core-banking/account-service.yaml  # restore the approved Service from git
kubectl -n core-banking get endpoints account-service        # 2 addresses
```

## Lessons
- A Service finds its pods **only** through its label selector. Change the
  selector without changing the pod labels (or the reverse) and the Service has
  zero endpoints. Every pod stays green while the service is completely down.
- `Running` + `Ready` is necessary, but it does not prove a service works.
  The synthetic traffic (customer-flow monitoring) caught this, not the pod dashboards.
- Selectors are an API contract: `spec.selector` on a Deployment is immutable
  for this reason. Guard Service selectors with GitOps review and with
  admission policies.
- Rename labels with expand/contract: add the new label to the pods, switch the
  selector, then remove the old label.
