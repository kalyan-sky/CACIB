# INC0048355 - reference solution

## Triage
```bash
./sim.sh status    # payments-gateway, paynow, lc-issuance DOWN; last error: upstream ... context deadline exceeded
kubectl -n payments exec deploy/payments-gateway -- \
  /txn-service probe http://fraud-screening.risk-compliance.svc.cluster.local:8080/healthz   # times out
kubectl -n risk-compliance get networkpolicy allow-from-consumers -o yaml
#   ingress.from.namespaceSelector.matchLabels: bank.sim/trust-zone: internal
kubectl get ns -L bank.sim/trust-zone      # no namespace has that label -> nothing is allowed in
```

## Fix A - restore the approved policy from git (fastest, lowest risk during a P1)
```bash
kubectl apply -f rendered/risk-compliance/00-namespace.yaml
```

## Fix B - implement the security team's intent correctly
Label only the namespaces that are real consumers. The inventory's upstreams
say which ones: `python3 tools/platform.py inventory`, or the git copy of the
policy, both list `payments` and `trade-finance`.
```bash
for ns in payments trade-finance; do
  kubectl label ns "$ns" bank.sim/trust-zone=internal --overwrite
done
```
Do **not** label every namespace, and do not delete `default-deny-ingress`.
The checker tests that `data-reporting` is still blocked.

## Lessons
- NetworkPolicies are **additive allow-lists**. With a default-deny in place, a
  wrong selector silently blocks traffic. Pods stay Ready, because kubelet
  probes come from the node and are always allowed.
- Test policies before rolling them out: run `kubectl exec ... probe` to check
  connectivity, use Cilium/Calico policy tracing, or put new policies in
  audit/log mode first.
- Generate policies from a dependency inventory (as `tools/platform.py` does
  from `upstreams`), so a policy never depends on someone's memory.
- Change management: a change touching tier-1 paths needs a tested rollback
  plan and post-implementation verification.
