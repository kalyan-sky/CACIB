1. Errors in many apps that share one upstream point to the upstream or the path to it, not the callers.
2. Prove it is the network: `kubectl -n payments exec deploy/payments-gateway -- /txn-service probe http://fraud-screening.risk-compliance.svc.cluster.local:8080/healthz`
   A *timeout* (not "connection refused") is typical of dropped packets.
3. `kubectl -n risk-compliance get networkpolicy` and `kubectl -n risk-compliance get networkpolicy allow-from-consumers -o yaml`
4. A `namespaceSelector` matches **labels on namespaces**: `kubectl get ns --show-labels`. Which namespaces carry the label the policy wants?
5. Two valid fixes: restore the explicit consumer list from git, or implement the security team's trust-zone intent correctly - for the right namespaces only.
