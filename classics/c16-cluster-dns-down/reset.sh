#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
replicas=$(cat "$SIM_STATE/classics/c16.coredns-replicas" 2>/dev/null || echo 2)
current=$(k -n kube-system get deploy coredns -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "")
if [ "${current:-x}" = 0 ]; then
  k -n kube-system scale deploy coredns --replicas="$replicas" >/dev/null
fi
rm -f "$SIM_STATE/classics/c16.coredns-replicas"
