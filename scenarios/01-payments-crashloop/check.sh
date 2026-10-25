#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
want="postgresql://payments-gateway-db.payments.db.bank.local:5432/payments_gateway"
got=$(jp '{.data.DB_URL}' configmap payments-gateway-config -n payments)
[ "$got" = "$want" ] && ok "ConfigMap DB_URL matches the approved value" ||
  ko "ConfigMap DB_URL is '${got:-<missing>}' - expected the value from rendered/payments/payments-gateway.yaml"
deploy_healthy payments payments-gateway && ok "payments-gateway: all replicas available" ||
  ko "payments-gateway is not fully available"
edge_txn payments payments-gateway >/dev/null && ok "end-to-end payment from the traffic tier succeeds" ||
  ko "end-to-end payment still failing"
check_result
