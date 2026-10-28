#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
k -n "$NS" exec deploy/frontend -- sh -c 'wget -qO- -T 3 "$BACKEND_URL"' >/dev/null 2>&1 &&
  ok "frontend reaches backend via \$BACKEND_URL" || ko "frontend still cannot reach the backend"
check_result
