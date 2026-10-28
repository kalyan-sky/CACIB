#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
deploy_healthy "$NS" web && ok "web is running and ready" || ko "web is not available"
conf=$(jp '{.data.default\.conf}' configmap web-config -n "$NS")
grep -q "/status" <<<"$conf" && ok "/status endpoint kept" || ko "the /status location was removed - fix the syntax, keep the feature"
check_result
