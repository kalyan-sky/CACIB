#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
eps=$(jp '{.subsets[*].addresses[*].ip}' endpoints account-service -n core-banking)
[ -n "$eps" ] && ok "account-service has endpoints ($eps)" || ko "account-service Service has no endpoints"
probe_from channels corporate-portal-api "$(svc core-banking account-service)/healthz" >/dev/null &&
  ok "corporate-portal-api -> account-service" || ko "corporate-portal-api cannot reach account-service"
edge_txn channels corporate-portal-api >/dev/null && ok "end-to-end portal transaction succeeds" ||
  ko "end-to-end portal transaction failing"
check_result
