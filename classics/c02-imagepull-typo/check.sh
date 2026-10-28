#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
deploy_healthy "$NS" api && ok "api is running" || ko "api is not available"
img=$(jp '{.spec.template.spec.containers[0].image}' deploy api -n "$NS")
[ "$img" = "nginx:1.27-alpine" ] && ok "image is nginx:1.27-alpine" || ko "image is '$img'"
check_result
