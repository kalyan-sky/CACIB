#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
[ -f "$SIM_STATE/07.vars" ] || die "start the scenario first: ./sim.sh start 07"
source "$SIM_STATE/07.vars"
started=$(cat "$SIM_STATE/07.started" 2>/dev/null || echo 0)
patched=$(jp '{.metadata.labels.bank\.sim/os-patch-level}' node "$NODE")
[ "$patched" = 2026-10 ] && ok "$NODE patched (os-patch-level=2026-10)" || ko "$NODE not patched yet (label bank.sim/os-patch-level=2026-10)"
[ -z "$(jp '{.spec.unschedulable}' node "$NODE")" ] && ok "$NODE uncordoned" || ko "$NODE is still cordoned"
# A drain evicts everything; any bank pod older than the scenario on that node means it was never drained.
stale=$(k get pods -A --field-selector "spec.nodeName=$NODE" -o json | "$PY" -c "
import json, sys, datetime
started = int(sys.argv[1])
skip = {'kube-system', 'local-path-storage', 'kube-public', 'kube-node-lease'}
n = 0
for p in json.load(sys.stdin)['items']:
    if p['metadata']['namespace'] in skip or any(o['kind'] == 'DaemonSet' for o in p['metadata'].get('ownerReferences', [])):
        continue
    ts = datetime.datetime.strptime(p['metadata']['creationTimestamp'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
    n += ts.timestamp() < started
print(n)" "$started")
[ "$stale" = 0 ] && ok "$NODE was drained" || ko "$stale workload pod(s) on $NODE predate the change - it was not drained"
pdbs=$(k get pdb -n core-banking -o json | "$PY" -c "
import json, sys
items = [p for p in json.load(sys.stdin)['items']
         if p['spec'].get('selector', {}).get('matchLabels', {}).get('app.kubernetes.io/name') == 'core-ledger']
print(len(items), min([p.get('status', {}).get('disruptionsAllowed', 0) for p in items] or [0]))")
read -r count allowed <<<"$pdbs"
[ "$count" -ge 1 ] && ok "core-ledger still protected by a PDB" || ko "core-ledger has no PodDisruptionBudget"
[ "$allowed" -ge 1 ] && ok "core-ledger PDB allows $allowed disruption(s)" || ko "core-ledger PDB allows 0 disruptions"
deploy_healthy core-banking core-ledger && ok "core-ledger fully available" || ko "core-ledger not fully available"
check_result
