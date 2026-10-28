#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
eps=$(jp '{.subsets[*].addresses[*].ip}' endpoints shop -n "$NS")
[ -n "$eps" ] && ok "shop has ready endpoints ($eps)" || ko "shop Service has no ready endpoints"
k -n "$NS" exec deploy/client -- wget -qO- -T 3 http://shop >/dev/null 2>&1 && ok "client -> shop works" ||
  ko "client cannot get a response from http://shop"
check_result
