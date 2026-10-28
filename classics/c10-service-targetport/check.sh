#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
k -n "$NS" exec deploy/client -- wget -qO- -T 3 http://catalog >/dev/null 2>&1 && ok "client -> catalog works" ||
  ko "client cannot get a response from http://catalog"
check_result
