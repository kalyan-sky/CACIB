#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
ns=shared-services app=notification-service
eps=$(jp '{.subsets[*].addresses[*].ip}' endpoints $app -n $ns)
[ -n "$eps" ] && ok "$app has ready endpoints ($eps)" || ko "$app Service has no ready endpoints"
deploy_healthy $ns $app && ok "$app: all replicas available" || ko "$app is not available"
probe_from risk-compliance kyc-onboarding "$(svc $ns $app)/healthz" >/dev/null &&
  ok "kyc-onboarding -> $app reachable" || ko "kyc-onboarding cannot reach $app"
strat=$(jp '{.spec.strategy.type}' deploy $app -n $ns)
[ "$strat" = RollingUpdate ] && ok "strategy is RollingUpdate" || ko "strategy is $strat - restore the platform standard"
edge_txn risk-compliance kyc-onboarding >/dev/null && ok "end-to-end KYC onboarding transaction succeeds" ||
  ko "end-to-end KYC onboarding transaction failing"
check_result
