#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
ns=trade-finance app=lc-issuance
deploy_healthy $ns $app && ok "$app rollout complete and available" || ko "$app rollout is not complete"
bad=$(jp '{range .items[*]}{.status.containerStatuses[0].state.waiting.reason}{"\n"}{end}' pods -n $ns -l app.kubernetes.io/name=$app | grep -cE 'ImagePull|ErrImage' || true)
[ "$bad" = 0 ] && ok "no pods failing to pull images" || ko "$bad pod(s) still failing to pull their image"
img=$(jp '{.spec.template.spec.containers[0].image}' deploy $app -n $ns)
echo "  (deployed image: $img)"
check_result
