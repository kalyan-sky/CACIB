#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
if grep -q "fx-options-pricer" "$ROOT/inventory/applications.yaml"; then
  die "revert your inventory change first: git checkout -- inventory/applications.yaml"
fi
k -n treasury-markets delete deploy,svc,configmap,pdb -l app.kubernetes.io/name=fx-options-pricer --ignore-not-found
"$PY" "$ROOT/tools/platform.py" render --profile "$(cat "$SIM_STATE/profile" 2>/dev/null || echo lite)" >/dev/null
restore treasury-markets channels/corporate-portal-api
