#!/usr/bin/env bash
# After `make gitops-bootstrap`: let Argo CD install the monitoring stack
# (Prometheus, Alertmanager, Grafana) and open Grafana to one IP address.
#
#   UI_IP=<your laptop's public IP> ./scripts/monitoring-up.sh
#
# Optional:
#   KPS_VERSION=<x.y.z>   kube-prometheus-stack chart version (default: the latest release)
source "$(dirname "$0")/lib.sh"

need terraform "https://developer.hashicorp.com/terraform/install"
need aws "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
need kubectl "https://kubernetes.io/docs/tasks/tools/"
[ -n "${UI_IP:-}" ] || die "set UI_IP=<the public IP of the machine with your browser> (https://checkip.amazonaws.com)"

REGION=$(tf_output region)
CLUSTER=$(tf_output cluster_name)
KUBE_CONTEXT="eks-$CLUSTER"
export KUBE_CONTEXT
aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" --alias "$KUBE_CONTEXT" >/dev/null
k get namespace argocd >/dev/null 2>&1 || die "Argo CD is not installed - run make gitops-bootstrap first"
[ "$(tf_output compute)" = ec2 ] || warn "this cluster runs on Fargate: node-exporter (a DaemonSet) and the Grafana load balancer won't work"

if [ -z "${KPS_VERSION:-}" ]; then
  # Releases are tagged <chart>-<version>, newest first. Pin what we install, so
  # the next upgrade is a deliberate change rather than a surprise.
  KPS_VERSION=$(curl -fsSL "https://api.github.com/repos/prometheus-community/helm-charts/releases?per_page=100" |
    grep -oE '"tag_name": *"kube-prometheus-stack-[0-9]+\.[0-9]+\.[0-9]+"' | head -1 |
    sed -E 's/.*-([0-9.]+)"$/\1/') || true
  [ -n "$KPS_VERSION" ] || die "could not look up the chart version - set KPS_VERSION=<x.y.z>"
fi

info "Grafana admin password (kept in Secret monitoring/grafana-admin)"
k create namespace monitoring --dry-run=client -o yaml | k apply -f - >/dev/null
if ! k -n monitoring get secret grafana-admin >/dev/null 2>&1; then
  k -n monitoring create secret generic grafana-admin --from-literal=admin-user=admin \
    --from-literal=admin-password="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 20)" >/dev/null
fi

info "Allowing Prometheus to scrape the bank (port 8080 from namespace monitoring)"
# The golden path renders this policy too (allow-from-monitoring); applying it
# here covers a gitops branch that predates it.
for ns in $(k get networkpolicy -A -o jsonpath='{range .items[?(@.metadata.name=="default-deny-ingress")]}{.metadata.namespace}{"\n"}{end}'); do
  k apply -f - >/dev/null <<YAML
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-from-monitoring
  namespace: $ns
spec:
  podSelector: {}
  policyTypes: [Ingress]
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: monitoring
      ports:
        - protocol: TCP
          port: 8080
YAML
  echo "    $ns"
done

info "Registering the monitoring application (kube-prometheus-stack $KPS_VERSION)"
sed "s#__KPS_VERSION__#$KPS_VERSION#g" "$ROOT/gitops/argocd/monitoring.yaml" | k apply -f - >/dev/null

info "Waiting for Argo CD to sync it (CRDs, operator, Prometheus, Grafana; 3-6 minutes)"
for _ in $(seq 90); do
  sync=$(jp '{.status.sync.status}' application monitoring -n argocd)
  health=$(jp '{.status.health.status}' application monitoring -n argocd)
  printf '\r    sync=%-10s health=%-12s' "${sync:-?}" "${health:-?}"
  [ "$sync" = Synced ] && [ "$health" = Healthy ] && break
  sleep 5
done
echo
[ "$health" = Healthy ] ||
  warn "not healthy yet - check: kubectl -n argocd get application monitoring -o jsonpath='{.status.conditions}'; kubectl -n monitoring get pods"

info "Bank alerts and dashboard"
for _ in $(seq 24); do k get crd prometheusrules.monitoring.coreos.com >/dev/null 2>&1 && break; sleep 5; done
k apply -f "$ROOT/platform/monitoring/bank-rules.yaml" >/dev/null
k -n monitoring create configmap bank-dashboard --from-file="$ROOT/platform/monitoring/bank-dashboard.json" \
  --dry-run=client -o yaml | k label --local -f - grafana_dashboard=1 -o yaml | k apply -f - >/dev/null

info "Exposing Grafana to $UI_IP only"
k apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Service
metadata:
  name: grafana-lb
  namespace: monitoring
spec:
  type: LoadBalancer
  loadBalancerSourceRanges: ["$UI_IP/32"]
  selector:
    app.kubernetes.io/name: grafana
    app.kubernetes.io/instance: monitoring
  ports:
    - name: http
      port: 80
      targetPort: 3000
YAML
for _ in $(seq 60); do
  host=$(jp '{.status.loadBalancer.ingress[0].hostname}' svc grafana-lb -n monitoring)
  [ -n "$host" ] && break
  sleep 5
done

argocd_host=$(jp '{.status.loadBalancer.ingress[0].hostname}' svc argocd-server-lb -n argocd)
cat <<MSG

    Grafana:   http://${host:-<pending: kubectl -n monitoring get svc grafana-lb>}   (DNS can take 2-3 minutes)
               admin / $(k -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d)
               Dashboards > "Bank - transactions overview", and the "Kubernetes / Compute Resources" set
    Argo CD:   ${argocd_host:+https://$argocd_host}${argocd_host:-not exposed (run make gitops-bootstrap with ARGOCD_UI_IP=$UI_IP)}
               apps "bank-sim" and "monitoring"
    Prometheus (no load balancer): kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090
MSG
