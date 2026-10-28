#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
eps=$(jp '{.subsets[*].addresses[*].ip}' endpoints orders -n "$NS")
[ -n "$eps" ] && ok "orders has endpoints ($eps)" || ko "orders Service has no endpoints"
k -n "$NS" exec deploy/client -- wget -qO- -T 3 http://orders >/dev/null 2>&1 && ok "client -> orders works" ||
  ko "client cannot reach http://orders"
check_result
