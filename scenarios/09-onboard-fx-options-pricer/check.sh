#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
ns=treasury-markets app=fx-options-pricer
"$PY" - <<PY && ok "$app is in the inventory" || ko "$app not found in inventory/applications.yaml (domain $ns)"
import yaml, sys
inv = yaml.safe_load(open("$ROOT/inventory/applications.yaml"))
sys.exit(0 if any(a["name"] == "$app" and a["domain"] == "$ns" for a in inv["applications"]) else 1)
PY
deploy_healthy $ns $app && ok "$app deployed and available" || ko "$app not deployed / not available"
tier=$(jp '{.spec.template.metadata.labels.bank\.sim/tier}' deploy $app -n $ns)
[ "$tier" = tier-2 ] && ok "labelled tier-2" || ko "tier label is '${tier:-missing}', expected tier-2"
req="$(jp '{.spec.template.spec.containers[0].resources.requests.cpu}' deploy $app -n $ns)/$(jp '{.spec.template.spec.containers[0].resources.requests.memory}' deploy $app -n $ns)"
[ "$req" = 50m/64Mi ] && ok "requests 50m / 64Mi" || ko "requests are $req, expected 50m/64Mi"
probe_from channels corporate-portal-api "$(svc $ns $app)/healthz" >/dev/null &&
  ok "corporate-portal-api -> $app allowed" || ko "corporate-portal-api cannot reach $app (NetworkPolicy? not rendered/applied?)"
out=$(edge_txn channels corporate-portal-api || true)
grep -q "$app" <<<"$out" && ok "portal transactions now flow through $app" ||
  ko "portal transactions do not call $app yet (portal UPSTREAMS / pods not rolled?)"
"$PY" "$ROOT/tools/platform.py" audit --live -n $ns >/dev/null && ok "platform control audit passes for $ns" ||
  ko "audit findings in $ns: python3 tools/platform.py audit --live -n $ns"
check_result
