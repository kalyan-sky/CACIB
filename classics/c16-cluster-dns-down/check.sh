#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
ready=$(k -n kube-system get deploy coredns -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
[ "${ready:-0}" -ge 1 ] && ok "CoreDNS has $ready ready replica(s)" || ko "CoreDNS has no ready replicas"
k -n "$NS" exec deploy/client -- timeout 5 nslookup kubernetes.default.svc.cluster.local >/dev/null 2>&1 &&
  ok "name resolution works from a pod" || ko "pods still cannot resolve names"
check_result
