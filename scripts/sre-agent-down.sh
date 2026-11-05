#!/usr/bin/env bash
# Remove the SRE agent and its Alertmanager routing. Incident history (its volume) is deleted too.
# DELETE_IMAGES=1 also deletes the sre-agent ECR repository.
source "$(dirname "$0")/lib.sh"
CLUSTER=$(tf_output cluster_name)
REGION=$(tf_output region)
KUBE_CONTEXT="eks-$CLUSTER"
export KUBE_CONTEXT

k -n argocd patch application monitoring --type merge \
  -p '{"spec":{"source":{"helm":{"valuesObject":{"alertmanager":null}}}}}' >/dev/null 2>&1 || true
k delete -f "$ROOT/platform/monitoring/k8s-incident-rules.yaml" --ignore-not-found >/dev/null
k -n monitoring delete secret sre-agent-webhook --ignore-not-found >/dev/null
k -n sre-agent delete svc sre-agent-dashboard --ignore-not-found --wait=true >/dev/null
k delete namespace sre-agent --ignore-not-found >/dev/null
k delete clusterrolebinding sre-agent-readonly --ignore-not-found >/dev/null
k delete clusterrole sre-agent-readonly --ignore-not-found >/dev/null
if [ "${DELETE_IMAGES:-0}" = 1 ]; then
  aws ecr delete-repository --repository-name sre-agent --region "$REGION" --force >/dev/null 2>&1 || true
fi
info "SRE agent removed"
