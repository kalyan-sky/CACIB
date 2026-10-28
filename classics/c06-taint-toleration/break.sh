#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
# pick a node that ordinary pods can use (no NoSchedule taints)
node=$(k get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.spec.taints[*].effect}{"\n"}{end}' |
  awk -F'|' '$2 !~ /NoSchedule/ {print $1; exit}')
[ -n "$node" ] || node=$(k get nodes -o jsonpath='{.items[0].metadata.name}')
mkdir -p "$SIM_STATE/classics"
echo "$node" >"$SIM_STATE/classics/c06.node"
k label node "$node" team=payments --overwrite
k taint node "$node" dedicated=payments:NoSchedule --overwrite
echo "NODE=$node" >"$SIM_STATE/classics/c06.vars"   # fills {{NODE}} in the ticket
