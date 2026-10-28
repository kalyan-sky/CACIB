#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
replicas=$(k -n kube-system get deploy coredns -o jsonpath='{.spec.replicas}')
mkdir -p "$SIM_STATE/classics"
echo "${replicas:-2}" >"$SIM_STATE/classics/c16.coredns-replicas"
# give the client a moment to start and cache nothing, then take DNS away
k -n "$NS" rollout status deploy/client --timeout=120s >/dev/null 2>&1 || true
k -n kube-system scale deploy coredns --replicas=0
