#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
ns=data-reporting
psa=$(jp '{.metadata.labels.pod-security\.kubernetes\.io/enforce}' ns $ns)
[ "$psa" = restricted ] && ok "PSA enforce=restricted" || ko "PSA enforce is '${psa:-unset}'"
deploy_healthy $ns mas-610-reporting && ok "mas-610-reporting healthy" || ko "mas-610-reporting not healthy"
if out=$("$PY" "$ROOT/tools/platform.py" audit --live -n $ns); then
  ok "live audit of $ns: no findings"
else
  ko "live audit of $ns still has findings:"
  grep '^\s*\[' <<<"$out" | sed 's/^/      /'
fi
check_result
