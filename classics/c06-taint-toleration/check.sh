#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
[ -f "$SIM_STATE/classics/c06.node" ] || die "start the case first: ./k8s-classics.sh start c06"
node=$(cat "$SIM_STATE/classics/c06.node")
deploy_healthy "$NS" payments-worker && ok "payments-worker is running" || ko "payments-worker is not running"
on=$(jp '{.items[0].spec.nodeName}' pods -n "$NS" -l app=payments-worker --field-selector=status.phase=Running)
[ "$on" = "$node" ] && ok "running on the dedicated node $node" || ko "not running on $node (on: ${on:-nothing})"
k get node "$node" -o jsonpath='{.spec.taints}' | grep -q '"key":"dedicated"' && ok "taint kept on $node" ||
  ko "the dedicated=payments taint was removed - tolerate it instead"
check_result
