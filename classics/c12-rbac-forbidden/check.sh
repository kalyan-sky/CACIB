#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
SA="system:serviceaccount:$NS:reporter"
can() { k auth can-i "$@" --as="$SA" 2>/dev/null; }
[ "$(can list pods -n "$NS")" = yes ] && ok "reporter can list pods in $NS" || ko "reporter cannot list pods in $NS"
[ "$(can delete pods -n "$NS")" = no ] && ok "cannot delete pods" || ko "reporter can delete pods - too much"
[ "$(can get secrets -n "$NS")" = no ] && ok "cannot read Secrets" || ko "reporter can read Secrets - too much"
[ "$(can list pods -n kube-system)" = no ] && ok "cannot read other namespaces" || ko "reporter can read other namespaces - too much"
k -n "$NS" logs deploy/pod-reporter --tail=1 2>/dev/null | grep -q "HTTP 200" && ok "reporter now gets HTTP 200" ||
  ko "the reporter's last call was not HTTP 200 yet (it retries every 10 s)"
check_result
