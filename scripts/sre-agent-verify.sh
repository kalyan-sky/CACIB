#!/usr/bin/env bash
# Verify the SRE agent on the live cluster without changing anything it shouldn't:
#   1. RBAC: the agent's ServiceAccount can read, and can NOT write, read Secrets or exec
#   2. the agent is healthy and Alertmanager routes alerts to it
#   --incident: also start classic case c07 (OOMKilled, its own namespace) and wait for
#               the agent to investigate it; reset it afterwards with ./k8s-classics.sh reset c07
source "$(dirname "$0")/lib.sh"

CLUSTER=$(tf_output cluster_name)
KUBE_CONTEXT="eks-$CLUSTER"
export KUBE_CONTEXT
SA="system:serviceaccount:sre-agent:sre-agent"

can() { k auth can-i "$@" --as="$SA" 2>/dev/null || true; }
expect() { # expect yes|no <verb> <resource> [-n ns]
  local want=$1; shift
  local got; got=$(can "$@")
  if [ "$got" = "$want" ]; then ok "can-i $* -> $got"; else ko "can-i $* -> $got (expected $want)"; fi
}

info "RBAC of $SA"
expect yes get pods -n payments
expect yes list pods -A
expect yes get pods --subresource=log -n payments
expect yes list events -n payments
expect yes get deployments -n payments
expect yes list nodes
expect yes list pods.metrics.k8s.io -n payments
expect no delete pods -n payments
expect no patch deployments -n payments
expect no update deployments -n payments
expect no create pods -n payments
expect no create pods --subresource=exec -n payments
expect no create pods --subresource=attach -n payments
expect no create pods --subresource=portforward -n payments
expect no get secrets -n payments
expect no list secrets -A
expect no get configmaps -n payments
expect no delete nodes
expect no patch nodes
expect no create serviceaccounts --subresource=token -n sre-agent
expect no escalate clusterroles
expect no bind clusterroles
expect no impersonate users
expect no create clusterrolebindings
k get clusterrolebinding -o json | python3 -c '
import json, sys
bad = [b["metadata"]["name"] for b in json.load(sys.stdin)["items"]
       if any(s.get("kind") == "ServiceAccount" and s.get("namespace") == "sre-agent" for s in b.get("subjects") or [])
       and b["roleRef"]["name"] != "sre-agent-readonly"]
print("  ✔ no other ClusterRoleBinding grants the agent anything" if not bad else "  ✘ extra bindings: " + ", ".join(bad))
sys.exit(1 if bad else 0)' || FAILED=1

info "Agent health"
# Through a port-forward: the agent's NetworkPolicy only admits the monitoring namespace,
# so the API server's service proxy can't reach it (and would hang).
port=$((20000 + RANDOM % 20000))
k -n sre-agent port-forward svc/sre-agent "$port:8080" >/dev/null 2>&1 &
pf=$!
trap 'kill $pf 2>/dev/null || true' EXIT
for _ in $(seq 20); do curl -s -m 2 "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && break; sleep 0.5; done
health=$(curl -s -m 5 "http://127.0.0.1:$port/healthz" || true)
[[ "$health" == *ok* ]] && ok "agent answers /healthz" || ko "agent does not answer (kubectl -n sre-agent logs deploy/sre-agent)"
am=$(k get --raw "/api/v1/namespaces/monitoring/services/monitoring-kube-prometheus-alertmanager:9093/proxy/api/v2/status" \
  --request-timeout=15s 2>/dev/null || true)
[[ "$am" == *sre-agent.sre-agent.svc* ]] && ok "Alertmanager routes alerts to the agent" ||
  ko "Alertmanager config has no sre-agent receiver yet (Argo CD may still be syncing; re-run in a minute)"
errs=$(curl -s -m 5 "http://127.0.0.1:$port/metrics" 2>/dev/null |
  awk '/^sre_agent_kubernetes_api_errors_total/ && /status="403"/ {s+=$2} END {print s+0}' || true)
[ "${errs:-0}" = 0 ] && ok "no 403s from the Kubernetes API so far" || warn "$errs Kubernetes 403s: RBAC is denying something the agent needs"
kill $pf 2>/dev/null || true

if [ "${1:-}" = "--incident" ]; then
  info "Test incident: classic case c07 (OOMKilled) in namespace c07-oomkilled"
  "$ROOT/k8s-classics.sh" start c07 >/dev/null
  echo "    waiting for: pod OOMKilled -> Prometheus rule PodOOMKilled -> Alertmanager (30s) -> agent (up to 6 min)"
  for _ in $(seq 72); do
    line=$(k -n sre-agent logs deploy/sre-agent --since=15m 2>/dev/null | grep '"incident investigated"' | grep 'c07-oomkilled' | tail -1 || true)
    [ -n "$line" ] && break
    sleep 5
  done
  if [ -n "$line" ]; then
    ok "agent investigated it: $(echo "$line" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(d["incident"], d["type"], d["status"], "confidence", d["confidence"], "email", d["email"])')"
    echo "    Open the dashboard and the email; then: ./k8s-classics.sh reset c07"
  else
    ko "no investigation seen yet. Check: kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-alertmanager 9093 (Alerts), and kubectl -n sre-agent logs deploy/sre-agent"
  fi
fi
check_result
