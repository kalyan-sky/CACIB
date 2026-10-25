#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
rc=risk-compliance
fraud="$(svc $rc fraud-screening)/healthz"
sanc="$(svc $rc sanctions-screening)/healthz"
probe_from payments payments-gateway "$fraud" >/dev/null && ok "payments-gateway -> fraud-screening" || ko "payments-gateway -> fraud-screening blocked"
probe_from payments swift-connector "$sanc" >/dev/null && ok "swift-connector -> sanctions-screening" || ko "swift-connector -> sanctions-screening blocked"
probe_from trade-finance lc-issuance "$sanc" >/dev/null && ok "lc-issuance -> sanctions-screening" || ko "lc-issuance -> sanctions-screening blocked"
dd=$(jp '{.spec.policyTypes}' networkpolicy default-deny-ingress -n $rc)
[ -n "$dd" ] && ok "default-deny-ingress still in place" || ko "default-deny-ingress was removed from $rc"
if probe_from data-reporting mas-610-reporting "$fraud" >/dev/null; then
  ko "data-reporting can reach fraud-screening - the fix is over-permissive (or your CNI does not enforce NetworkPolicy)"
else
  ok "non-consumer namespace data-reporting is still blocked"
fi
check_result
