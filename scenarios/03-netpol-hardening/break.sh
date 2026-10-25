#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
k apply -f - <<'YAML'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-from-consumers
  namespace: risk-compliance
  annotations:
    bank.sim/change: "CHG0031902 zero-trust hardening"
spec:
  podSelector: {}
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              bank.sim/trust-zone: internal
      ports:
        - {protocol: TCP, port: 8080}
YAML
