#!/usr/bin/env bash
# Certificate incident test: real HTTPS servers with bad certificates, so the monitoring raises
# alerts and the SRE agent investigates, emails and drafts the RCA.
#
#   ./scripts/cert-lab.sh start    3 web servers in namespace cert-lab:
#                                    web-healthy    valid certificate            -> no alert (control)
#                                    web-expiring   valid, expires in 1 day      -> CertificateExpiringCritical
#                                    web-wronghost  issued for other hostnames   -> TLSEndpointHandshakeFailing
#   ./scripts/cert-lab.sh status   pods, certificates as served, firing certificate alerts
#   ./scripts/cert-lab.sh renew    the "fix": correct certificates + restart -> alerts resolve, RCA emails
#   ./scripts/cert-lab.sh sign <file.csr>   act as the lab CA: sign a CSR (names taken from it) -> <file>.crt
#                                           (the "send new.csr to the CA" step of the agent's fix script)
#   ./scripts/cert-lab.sh reset    delete the test (keeps the lab CA)
#
# Needs make cert-monitoring first (it creates the lab CA in ~/.cert-lab).
source "$(dirname "$0")/lib.sh"

need openssl "sudo apt-get install -y openssl"
CLUSTER=$(tf_output cluster_name)
KUBE_CONTEXT="eks-$CLUSTER"
export KUBE_CONTEXT
NS=cert-lab
CA_DIR="${CERT_LAB_CA_DIR:-$HOME/.cert-lab}"
APPS="web-healthy web-expiring web-wronghost"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

host() { echo "$1.$NS.svc"; }

# issue <name> <days> <dns names...>: certificate signed by the lab CA
issue() {
  local name=$1 days=$2; shift 2
  local san; san=$(printf 'DNS:%s,' "$@"); san=${san%,}
  openssl req -new -newkey rsa:2048 -nodes -subj "/CN=$1" -keyout "$WORK/$name.key" -out "$WORK/$name.csr" 2>/dev/null
  printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n' \
    "$san" >"$WORK/$name.ext"
  openssl x509 -req -in "$WORK/$name.csr" -CA "$CA_DIR/ca.crt" -CAkey "$CA_DIR/ca.key" -CAcreateserial \
    -days "$days" -sha256 -extfile "$WORK/$name.ext" -out "$WORK/$name.crt" 2>/dev/null
  k -n "$NS" create secret tls "$name-tls" --cert="$WORK/$name.crt" --key="$WORK/$name.key" \
    --dry-run=client -o yaml | k label --local -f - app.kubernetes.io/part-of=cert-lab -o yaml | k apply -f - >/dev/null
  printf '    %-14s %-34s expires %s\n' "$name" "$(echo "$@" | cut -d' ' -f1)" \
    "$(openssl x509 -noout -enddate -in "$WORK/$name.crt" | cut -d= -f2)"
}

cmd_start() {
  [ -f "$CA_DIR/ca.key" ] || die "no lab CA in $CA_DIR - run make cert-monitoring first"
  k get application blackbox-exporter -n argocd >/dev/null 2>&1 || die "run make cert-monitoring first"
  info "Namespace $NS and certificates (signed by the lab CA)"
  k create namespace "$NS" --dry-run=client -o yaml | k apply -f - >/dev/null
  issue web-healthy 90 "$(host web-healthy)" "$(host web-healthy).cluster.local"
  issue web-expiring 1 "$(host web-expiring)" "$(host web-expiring).cluster.local"
  issue web-wronghost 90 payments.wrong.example api.wrong.example

  info "Web servers (nginx, TLS on 8443) and their Services (443)"
  k -n "$NS" apply -f - >/dev/null <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: nginx-tls
  labels: {app.kubernetes.io/part-of: cert-lab}
data:
  nginx.conf: |
    events {}
    http {
      server {
        listen 8443 ssl;
        ssl_certificate     /etc/tls/tls.crt;
        ssl_certificate_key /etc/tls/tls.key;
        location / { return 200 "ok\n"; }
      }
    }
YAML
  for app in $APPS; do
    k -n "$NS" apply -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $app
  labels: {app.kubernetes.io/part-of: cert-lab}
spec:
  replicas: 1
  selector: {matchLabels: {app: $app}}
  template:
    metadata:
      labels: {app: $app}
    spec:
      containers:
        - name: nginx
          image: nginx:1.27-alpine
          ports: [{name: https, containerPort: 8443}]
          resources:
            requests: {cpu: 10m, memory: 16Mi}
            limits: {cpu: 100m, memory: 64Mi}
          volumeMounts:
            - {name: tls, mountPath: /etc/tls, readOnly: true}
            - {name: conf, mountPath: /etc/nginx/nginx.conf, subPath: nginx.conf}
      volumes:
        - name: tls
          secret: {secretName: $app-tls}
        - name: conf
          configMap: {name: nginx-tls}
---
apiVersion: v1
kind: Service
metadata:
  name: $app
  labels: {app.kubernetes.io/part-of: cert-lab}
spec:
  selector: {app: $app}
  ports: [{name: https, port: 443, targetPort: https}]
YAML
    # Two checks per endpoint: strict (as a verifying client) and reachability (handshake only)
    for module in tls_strict tls_reachability; do
      job=${module//_/-}
      k apply -f - >/dev/null <<YAML
apiVersion: monitoring.coreos.com/v1
kind: Probe
metadata:
  name: cert-lab-$app-$job
  namespace: monitoring
  labels: {app.kubernetes.io/part-of: cert-lab}
spec:
  jobName: $job
  interval: 30s
  module: $module
  prober:
    url: blackbox-exporter.monitoring.svc:9115
  targets:
    staticConfig:
      static: [https://$(host "$app"):443]
      labels:
        target_namespace: $NS
        deployment: $app
        secret: $app-tls
YAML
    done
  done
  k -n "$NS" rollout status deploy --timeout=120s >/dev/null
  ok "3 HTTPS endpoints running"
  cat <<MSG

    What happens next (no action needed):
      ~1-2 min  x509-certificate-exporter sees web-expiring-tls expiring in 1 day; the blackbox probe sees
                that web-wronghost's certificate is for payments.wrong.example
      ~5-6 min  CertificateExpiringCritical (web-expiring) fires; TLSEndpointHandshakeFailing (web-wronghost)
                fires after 3 min; TLSEndpointCertExpiringSoon (web-expiring) after 15 min, joining the same incident
      +1 min    the agent investigates read-only (TLS handshake, who uses the Secret) -> 2 incident emails
    Watch:   ./scripts/cert-lab.sh status      Dashboard: incidents CertificateExpiringSoon and TLSHostnameMismatch
    Then fix it as an engineer would (the agent's guide), or: ./scripts/cert-lab.sh renew
MSG
}

cmd_status() {
  info "Pods"
  k -n "$NS" get pods -o wide 2>/dev/null || echo "    namespace $NS not found"
  info "Certificates in the Secrets (what the exporter reads)"
  for app in $APPS; do
    k -n "$NS" get secret "$app-tls" -o jsonpath='{.data.tls\.crt}' 2>/dev/null | base64 -d 2>/dev/null |
      openssl x509 -noout -enddate -ext subjectAltName 2>/dev/null | tr '\n' ' ' | sed "s/^/    $app: /"
    echo
  done
  info "Firing certificate alerts"
  local port=$((20000 + RANDOM % 20000))
  k -n monitoring port-forward svc/monitoring-kube-prometheus-alertmanager "$port:9093" >/dev/null 2>&1 &
  local pf=$!
  sleep 3
  curl -s "http://127.0.0.1:$port/api/v2/alerts?active=true" | python3 -c '
import json, sys
rows = [a for a in json.load(sys.stdin) if a["labels"]["alertname"].startswith(("Certificate", "TLS"))]
for a in rows:
    l = a["labels"]
    print("    %-30s %-12s %s" % (l["alertname"], l.get("namespace", "-"), l.get("secret", l.get("instance", "-"))))
print("    none yet (alerts need ~5 min after start)" if not rows else "")' || echo "    (Alertmanager not reachable)"
  kill $pf 2>/dev/null || true
}

cmd_renew() {
  [ -f "$CA_DIR/ca.key" ] || die "no lab CA in $CA_DIR"
  info "Renewing: correct names, 90 days (what the RCA's resolution guide tells the engineer to do)"
  issue web-expiring 90 "$(host web-expiring)" "$(host web-expiring).cluster.local"
  issue web-wronghost 90 "$(host web-wronghost)" "$(host web-wronghost).cluster.local"
  k -n "$NS" rollout restart deployment/web-expiring deployment/web-wronghost >/dev/null
  k -n "$NS" rollout status deploy --timeout=120s >/dev/null
  ok "renewed and restarted. Alerts resolve within ~2-5 min; then the RCA-for-review emails arrive."
  echo "    Complete and sign off each RCA in the dashboard (Incident > Root cause analysis)."
}

cmd_sign() {
  local csr=${1:?usage: ./scripts/cert-lab.sh sign <path/to/new.csr>}
  [ -f "$csr" ] || die "$csr not found"
  [ -f "$CA_DIR/ca.key" ] || die "no lab CA in $CA_DIR - run make cert-monitoring first"
  local crt="${csr%.csr}.crt"
  openssl req -in "$csr" -noout -verify >/dev/null 2>&1 || die "$csr is not a valid certificate request"
  printf 'extendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n' \
    >"$WORK/sign.ext"
  # the names (subjectAltName) come from the request itself
  openssl x509 -req -in "$csr" -CA "$CA_DIR/ca.crt" -CAkey "$CA_DIR/ca.key" -CAcreateserial -days 90 -sha256 \
    -copy_extensions copy -extfile "$WORK/sign.ext" -out "$crt" 2>/dev/null || die "signing failed"
  ok "signed by $(openssl x509 -noout -issuer -in "$crt" | sed 's/issuer=//') -> $crt"
  openssl x509 -noout -subject -enddate -ext subjectAltName -in "$crt" | sed 's/^/    /'
}

cmd_reset() {
  k delete probes -n monitoring -l app.kubernetes.io/part-of=cert-lab --ignore-not-found >/dev/null
  k delete namespace "$NS" --ignore-not-found --wait=false >/dev/null
  ok "cert-lab removed (the lab CA stays in $CA_DIR)"
}

case "${1:-}" in
  start) cmd_start ;;
  status) cmd_status ;;
  renew) cmd_renew ;;
  sign) cmd_sign "${2:-}" ;;
  reset) cmd_reset ;;
  *) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0" ;;
esac
