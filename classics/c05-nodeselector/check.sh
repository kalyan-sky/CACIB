#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
deploy_healthy "$NS" analytics && ok "analytics is running" || ko "analytics is not running"
check_result
