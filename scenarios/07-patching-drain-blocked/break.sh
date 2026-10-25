#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
node=$(jp '{.items[0].spec.nodeName}' pods -n core-banking -l app.kubernetes.io/name=core-ledger)
[ -n "$node" ] || die "no core-ledger pod found"
mkdir -p "$SIM_STATE"
echo "NODE=$node" >"$SIM_STATE/07.vars"
k -n core-banking patch pdb core-ledger --type=merge -p '{"spec":{"maxUnavailable":null,"minAvailable":2}}'
k label node "$node" bank.sim/os-patch-level- >/dev/null 2>&1 || true
