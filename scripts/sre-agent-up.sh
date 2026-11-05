#!/usr/bin/env bash
# Install the read-only SRE agent into the Terraform EKS cluster (run on the jump host,
# after make gitops-bootstrap and make monitoring).
#
#   SMTP_USER=<you>@gmail.com EMAIL_TO=<you>@gmail.com ./scripts/sre-agent-up.sh
#
# It asks for the Gmail App Password and a dashboard password (never echoed, never
# written to disk outside Kubernetes Secrets).
#
# Optional:
#   DASHBOARD_IP=<laptop IP>  open the dashboard on an HTTPS load balancer for that IP only
#   IMAGE=<image>             skip the build and deploy this image
#   APPROVER=<name>           dashboard user with the approver role (default: your Linux user)
#   ENVIRONMENT=prod|uat|dev  environment name in incidents and email routing (default: dev)
source "$(dirname "$0")/lib.sh"

need terraform "https://developer.hashicorp.com/terraform/install"
need aws "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
need kubectl "https://kubernetes.io/docs/tasks/tools/"
need openssl "sudo apt-get install -y openssl"
[ -n "${SMTP_USER:-}" ] || die "set SMTP_USER=<the Gmail address that sends the emails>"
EMAIL_TO="${EMAIL_TO:-$SMTP_USER}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
APPROVER="${APPROVER:-$(id -un)}"

REGION=$(tf_output region)
CLUSTER=$(tf_output cluster_name)
KUBE_CONTEXT="eks-$CLUSTER"
export KUBE_CONTEXT
aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" --alias "$KUBE_CONTEXT" >/dev/null
k get namespace monitoring >/dev/null 2>&1 || die "the monitoring stack is not installed - run make monitoring first"
[ "$(tf_output compute)" = ec2 ] || die "the agent stores incidents on an EBS volume: use compute = \"ec2\""
DEPLOY="$ROOT/apps/sre-agent/deploy"

# ------------------------------------------------------------------ image
if [ -z "${IMAGE:-}" ]; then
  python3 -m pip --version >/dev/null 2>&1 || die "pip is needed to build the image: sudo apt-get install -y python3-pip"
  account=$(aws sts get-caller-identity --query Account --output text)
  repo="$account.dkr.ecr.$REGION.amazonaws.com/sre-agent"
  aws ecr describe-repositories --repository-names sre-agent --region "$REGION" >/dev/null 2>&1 ||
    aws ecr create-repository --repository-name sre-agent --region "$REGION" \
      --image-scanning-configuration scanOnPush=true --image-tag-mutability IMMUTABLE >/dev/null
  IMAGE="$repo:$(git -C "$ROOT" rev-parse --short HEAD)-$(date +%Y%m%d%H%M)"
  info "Building $IMAGE"
  "$ROOT/ci/build-sre-agent.sh" "$IMAGE"
fi

# ------------------------------------------------------------------ secrets (asked once, kept in Kubernetes)
info "Namespace, ServiceAccount and read-only ClusterRole"
k apply -f "$DEPLOY/00-namespace.yaml" -f "$DEPLOY/rbac.yaml" >/dev/null

if ! k -n sre-agent get secret sre-agent-secrets >/dev/null 2>&1; then
  info "Secrets (stored only in the sre-agent-secrets Secret)"
  read -r -s -p "    Gmail App Password for $SMTP_USER (16 letters, spaces are fine): " smtp_password; echo
  smtp_password=${smtp_password// /}
  [ -n "$smtp_password" ] || die "an App Password is required (Google account > Security > App passwords)"
  read -r -s -p "    New dashboard password for '$APPROVER' (12+ characters): " dash_password; echo
  [ "${#dash_password}" -ge 12 ] || die "use at least 12 characters"
  approver_line=$(PYTHONPATH="$ROOT/apps/sre-agent" python3 -c \
    "import sys; from sre_agent.auth import hash_password; print(sys.argv[1] + ':approver:' + hash_password(sys.argv[2]))" \
    "$APPROVER" "$dash_password")
  unset dash_password
  k -n sre-agent create secret generic sre-agent-secrets \
    --from-literal=webhook-token="$(openssl rand -hex 32)" \
    --from-literal=session-secret="$(openssl rand -hex 32)" \
    --from-literal=smtp-password="$smtp_password" \
    --from-literal=approvers="$approver_line" >/dev/null
  unset smtp_password approver_line
else
  info "Reusing the existing sre-agent-secrets (delete it to change passwords)"
fi
# Alertmanager authenticates to the agent with the same webhook token
token=$(k -n sre-agent get secret sre-agent-secrets -o jsonpath='{.data.webhook-token}' | base64 -d)
k -n monitoring create secret generic sre-agent-webhook --from-literal=token="$token" \
  --dry-run=client -o yaml | k apply -f - >/dev/null
unset token

if ! k -n sre-agent get secret sre-agent-tls >/dev/null 2>&1; then
  tmp=$(mktemp -d)
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=sre-agent" \
    -keyout "$tmp/tls.key" -out "$tmp/tls.crt" 2>/dev/null
  k -n sre-agent create secret tls sre-agent-tls --cert="$tmp/tls.crt" --key="$tmp/tls.key" >/dev/null
  rm -rf "$tmp"
fi

# ------------------------------------------------------------------ agent
dashboard_url=""
if [ -n "${DASHBOARD_IP:-}" ]; then
  k apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Service
metadata:
  name: sre-agent-dashboard
  namespace: sre-agent
spec:
  type: LoadBalancer
  loadBalancerSourceRanges: ["$DASHBOARD_IP/32"]
  selector:
    app.kubernetes.io/name: sre-agent
  ports:
    - name: https
      port: 443
      targetPort: 8443
YAML
  for _ in $(seq 60); do
    host=$(jp '{.status.loadBalancer.ingress[0].hostname}' svc sre-agent-dashboard -n sre-agent)
    [ -n "$host" ] && break
    sleep 5
  done
  dashboard_url="https://${host:-}"
fi

info "Deploying the agent ($CLUSTER, environment $ENVIRONMENT)"
for f in config.yaml deployment.yaml networkpolicy.yaml monitoring.yaml; do
  sed -e "s#__IMAGE__#$IMAGE#" -e "s#__CLUSTER__#$CLUSTER#" -e "s#__ENVIRONMENT__#$ENVIRONMENT#" \
      -e "s#__SMTP_USER__#$SMTP_USER#" -e "s#__EMAIL_TO__#$EMAIL_TO#" -e "s#__DASHBOARD_URL__#$dashboard_url#" \
      "$DEPLOY/$f" | k apply -f - >/dev/null
done
k -n sre-agent rollout restart deployment/sre-agent >/dev/null 2>&1 || true  # pick up config changes
k -n sre-agent rollout status deployment/sre-agent --timeout=300s

# ------------------------------------------------------------------ alerts -> agent
info "Incident alert rules and Alertmanager routing to the agent"
k apply -f "$ROOT/platform/monitoring/k8s-incident-rules.yaml" >/dev/null
patch=$(cat <<JSON
{"spec":{"source":{"helm":{"valuesObject":{"alertmanager":{
  "alertmanagerSpec":{"secrets":["sre-agent-webhook"]},
  "config":{
    "global":{"resolve_timeout":"5m"},
    "route":{"receiver":"sre-agent","group_by":["alertname","namespace"],
             "group_wait":"30s","group_interval":"5m","repeat_interval":"4h",
             "routes":[{"receiver":"null","matchers":["alertname=~\"Watchdog|InfoInhibitor\""]}]},
    "inhibit_rules":[
      {"source_matchers":["severity=\"critical\""],"target_matchers":["severity=~\"warning|info\""],"equal":["namespace","alertname"]},
      {"source_matchers":["severity=\"warning\""],"target_matchers":["severity=\"info\""],"equal":["namespace","alertname"]},
      {"source_matchers":["alertname=\"InfoInhibitor\""],"target_matchers":["severity=\"info\""],"equal":["namespace"]},
      {"source_matchers":["alertname=\"CoreDNSDown\""],"target_matchers":["alertname=\"CoreDNSErrorsHigh\""]}],
    "receivers":[
      {"name":"null"},
      {"name":"sre-agent","webhook_configs":[{
        "url":"http://sre-agent.sre-agent.svc:8080/api/v1/alerts?cluster=$CLUSTER",
        "send_resolved":true,"max_alerts":50,
        "http_config":{"authorization":{"type":"Bearer",
          "credentials_file":"/etc/alertmanager/secrets/sre-agent-webhook/token"}}}]}]}}}}}}}
JSON
)
k -n argocd patch application monitoring --type merge -p "$patch" >/dev/null
info "Waiting for Argo CD to roll out the new Alertmanager configuration"
for _ in $(seq 60); do
  sync=$(jp '{.status.sync.status}' application monitoring -n argocd)
  [ "$sync" = Synced ] && break
  sleep 5
done

"$ROOT/scripts/sre-agent-verify.sh" || warn "verification reported problems (see above)"

cat <<MSG

    SRE agent is running (read-only). Image: $IMAGE
    Dashboard:  ${dashboard_url:-not exposed. On the jump host: kubectl -n sre-agent port-forward svc/sre-agent 8080}
                user: $APPROVER (approver), the password you just chose
    Emails:     $EMAIL_TO (from $SMTP_USER), severity MEDIUM and above
    Test it:    ./scripts/sre-agent-verify.sh --incident   (starts classic case c07 OOMKilled in its own namespace)
MSG
