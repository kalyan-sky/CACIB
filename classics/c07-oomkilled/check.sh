#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
deploy_healthy "$NS" cache-warmer && ok "cache-warmer is running" || ko "cache-warmer is not running"
r=$(jp '{.items[0].status.containerStatuses[0].restartCount}' pods -n "$NS" -l app=cache-warmer --field-selector=status.phase=Running)
[ "${r:-1}" = 0 ] && ok "no restarts since the fix" || ko "the current pod has restarted ${r:-?} time(s)"
k -n "$NS" logs deploy/cache-warmer 2>/dev/null | grep -q "cache warmed" && ok "cache loaded" || ko "cache not loaded yet"
check_result
