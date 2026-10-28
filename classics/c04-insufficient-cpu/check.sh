#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
deploy_healthy "$NS" reports && ok "reports is running" || ko "reports is not running"
cpu=$(jp '{.spec.template.spec.containers[0].resources.requests.cpu}' deploy reports -n "$NS")
m=$(case "$cpu" in *m) echo "${cpu%m}" ;; "") echo 0 ;; *) echo $((${cpu%.*} * 1000)) ;; esac)
[ "$m" -le 500 ] && ok "CPU request $cpu (<= 500m)" || ko "CPU request $cpu is still oversized"
check_result
