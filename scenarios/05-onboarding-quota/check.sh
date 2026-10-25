#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
ns=trade-finance app=trade-docs-ocr
deploy_healthy $ns $app && ok "$app: 2/2 available" || ko "$app is not fully available"
cpu=$(jp '{.spec.template.spec.containers[0].resources.requests.cpu}' deploy $app -n $ns)
mem=$(jp '{.spec.template.spec.containers[0].resources.requests.memory}' deploy $app -n $ns)
"$PY" - "$cpu" "$mem" <<'PY' && ok "requests right-sized ($cpu / $mem)" || ko "requests $cpu / $mem exceed the 250m / 256Mi policy"
import re, sys
cpu, mem = sys.argv[1], sys.argv[2]
if not cpu or not mem:
    sys.exit(1)
cpu_m = int(cpu[:-1]) if cpu.endswith("m") else float(cpu) * 1000
num, unit = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", mem).groups()
mem_mi = float(num) * {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "M": 1, "G": 1000, "": 1 / 1048576}[unit]
sys.exit(0 if cpu_m <= 250 and mem_mi <= 256 else 1)
PY
live=$(jp '{.spec.hard.requests\.cpu}' resourcequota compute-quota -n $ns)
approved=$("$PY" -c "
import yaml,sys
for d in yaml.safe_load_all(open('$RENDERED/$ns/00-namespace.yaml')):
    if d and d['kind'] == 'ResourceQuota': print(d['spec']['hard']['requests.cpu'])")
[ "$live" = "$approved" ] && ok "ResourceQuota unchanged ($live)" || ko "ResourceQuota requests.cpu changed: $live (approved: $approved)"
check_result
