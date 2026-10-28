#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
deploy_healthy "$NS" ledger && ok "ledger is running and ready" || ko "ledger is not ready (it needs ~25 s after each start)"
r=$(jp '{.items[0].status.containerStatuses[0].restartCount}' pods -n "$NS" -l app=ledger --field-selector=status.phase=Running)
[ "${r:-1}" = 0 ] && ok "no restarts" || ko "the current pod restarted ${r:-?} time(s)"
live=$(jp '{.spec.template.spec.containers[0].livenessProbe}' deploy ledger -n "$NS")
[ -n "$live" ] && ok "liveness probe still in place" || ko "the liveness probe was removed - hangs would no longer be detected"
check_result
