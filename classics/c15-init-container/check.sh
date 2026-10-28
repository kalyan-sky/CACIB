#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
deploy_healthy "$NS" payments-api && ok "payments-api is running" || ko "payments-api is not running (still in Init?)"
check_result
