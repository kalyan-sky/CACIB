#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
deploy_healthy "$NS" billing && ok "billing is running" || ko "billing is not running"
pw=$(k -n "$NS" exec deploy/billing -- sh -c 'printf %s "$DB_PASSWORD"' 2>/dev/null || true)
[ -n "$pw" ] && ok "DB_PASSWORD is set from the Secret" || ko "DB_PASSWORD is empty or the pod is not running"
check_result
