#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
phase=$(jp '{.status.phase}' pvc documents -n "$NS")
[ "$phase" = Bound ] && ok "PVC documents is Bound" || ko "PVC documents is ${phase:-missing}"
deploy_healthy "$NS" docstore && ok "docstore is running" || ko "docstore is not running"
check_result
