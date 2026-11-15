#!/usr/bin/env bash
# Certificate monitoring: Argo CD installs x509-certificate-exporter and the blackbox
# exporter, and the certificate alert rules route to the SRE agent. Run on the jump host
# after make monitoring and make sre-agent-up.
#
#   ./scripts/cert-monitoring-up.sh
#
# Trust: the TLS checks must trust the CA that issues your internal certificates.
#   TRUST_BUNDLE=<file>   PEM file with your internal root CA(s) (e.g. the bank's root)
#   (default)             a lab CA created in ~/.cert-lab (its key never leaves the jump host)
# Optional: X509_CHART_VERSION, BLACKBOX_CHART_VERSION (default: the latest releases, then pinned)
source "$(dirname "$0")/lib.sh"

need kubectl "https://kubernetes.io/docs/tasks/tools/"
need openssl "sudo apt-get install -y openssl"
CLUSTER=$(tf_output cluster_name)
REGION=$(tf_output region)
KUBE_CONTEXT="eks-$CLUSTER"
export KUBE_CONTEXT
aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" --alias "$KUBE_CONTEXT" >/dev/null
k get application monitoring -n argocd >/dev/null 2>&1 || die "run make monitoring first"
CA_DIR="${CERT_LAB_CA_DIR:-$HOME/.cert-lab}"

# ------------------------------------------------------------------ trust bundle (public certificates only)
if [ -n "${TRUST_BUNDLE:-}" ]; then
  [ -f "$TRUST_BUNDLE" ] || die "TRUST_BUNDLE $TRUST_BUNDLE not found"
  bundle="$TRUST_BUNDLE"
else
  if [ ! -f "$CA_DIR/ca.crt" ]; then
    info "Creating the lab internal CA in $CA_DIR (the key stays on this host)"
    mkdir -p "$CA_DIR" && chmod 700 "$CA_DIR"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=Bank Lab Internal CA" \
      -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
      -addext "subjectKeyIdentifier=hash" -keyout "$CA_DIR/ca.key" -out "$CA_DIR/ca.crt" 2>/dev/null
    chmod 600 "$CA_DIR/ca.key"
  fi
  bundle="$CA_DIR/ca.crt"
fi
info "Trust bundle for the TLS checks: $(openssl x509 -noout -subject -in "$bundle" | sed 's/subject=//')"
for ns in monitoring sre-agent; do
  k get namespace "$ns" >/dev/null 2>&1 || continue
  k -n "$ns" create secret generic tls-trust-bundle --from-file=ca.crt="$bundle" \
    --dry-run=client -o yaml | k apply -f - >/dev/null
done

# ------------------------------------------------------------------ chart versions (pinned at install)
latest_github_tag() { # owner/repo tag-prefix
  curl -fsSL "https://api.github.com/repos/$1/git/matching-refs/tags/$2" | python3 -c '
import json, re, sys
prefix = sys.argv[1]
vs = [r["ref"].rsplit("/", 1)[1][len(prefix):] for r in json.load(sys.stdin)]
vs = [v for v in vs if re.fullmatch(r"\d+\.\d+\.\d+", v)]
print(max(vs, key=lambda v: tuple(map(int, v.split(".")))) if vs else "")' "$2"
}
latest_index_version() { # index-url chart
  curl -fsSL "$1" | python3 -c '
import re, sys, yaml
entries = (yaml.safe_load(sys.stdin) or {}).get("entries", {}).get(sys.argv[1], [])
vs = [e["version"] for e in entries if re.fullmatch(r"\d+\.\d+\.\d+", str(e.get("version")))]
print(max(vs, key=lambda v: tuple(map(int, v.split(".")))) if vs else "")' "$2"
}
X509_CHART_VERSION="${X509_CHART_VERSION:-$(latest_index_version https://charts.enix.io/index.yaml x509-certificate-exporter || true)}"
BLACKBOX_CHART_VERSION="${BLACKBOX_CHART_VERSION:-$(latest_github_tag prometheus-community/helm-charts prometheus-blackbox-exporter- || true)}"
[ -n "$X509_CHART_VERSION" ] || die "could not look up x509-certificate-exporter - set X509_CHART_VERSION=<x.y.z>"
[ -n "$BLACKBOX_CHART_VERSION" ] || die "could not look up the blackbox exporter chart - set BLACKBOX_CHART_VERSION=<x.y.z>"

# ------------------------------------------------------------------ Argo CD: project, Prometheus Probe selection, apps
info "Allowing the platform project to use charts.enix.io; Prometheus picks up Probe objects"
k -n argocd patch appproject platform --type merge -p \
  '{"spec":{"sourceRepos":["https://prometheus-community.github.io/helm-charts","https://charts.enix.io"]}}' >/dev/null
k -n argocd patch application monitoring --type merge -p \
  '{"spec":{"source":{"helm":{"valuesObject":{"prometheus":{"prometheusSpec":{"probeSelectorNilUsesHelmValues":false}}}}}}}' >/dev/null

info "Installing x509-certificate-exporter $X509_CHART_VERSION and blackbox exporter $BLACKBOX_CHART_VERSION"
sed -e "s#__X509_VERSION__#$X509_CHART_VERSION#" -e "s#__BLACKBOX_VERSION__#$BLACKBOX_CHART_VERSION#" \
  "$ROOT/gitops/argocd/cert-monitoring.yaml" | k apply -f - >/dev/null
k apply -f "$ROOT/platform/monitoring/certificate-rules.yaml" >/dev/null

for app in x509-certificate-exporter blackbox-exporter; do
  for _ in $(seq 60); do
    sync=$(jp '{.status.sync.status}' application "$app" -n argocd)
    health=$(jp '{.status.health.status}' application "$app" -n argocd)
    [ "$sync" = Synced ] && [ "$health" = Healthy ] && break
    sleep 5
  done
  if [ "$health" = Healthy ]; then ok "$app: Synced / Healthy"; else
    ko "$app: sync=$sync health=$health (kubectl -n argocd get application $app -o jsonpath='{.status.conditions}')"; fi
done

# the agent reads the trust bundle at each check; restart only to pick up a new image or RBAC
k -n sre-agent get deploy sre-agent >/dev/null 2>&1 ||
  warn "the SRE agent is not installed: alerts will fire but nobody investigates them (make sre-agent-up)"

cat <<MSG

    Certificate monitoring is on. Every kubernetes.io/tls Secret is now checked for expiry, and the
    endpoints listed in Probe objects are checked like a client would (try: kubectl get probes -A).
    Alerts: CertificateExpiringSoon (<30 d), CertificateExpiringCritical (<3 d), CertificateExpired,
            TLSEndpointHandshakeFailing, TLSEndpointCertExpiringSoon, TLSEndpointDown.
    Test it: ./scripts/cert-lab.sh start
MSG
check_result
