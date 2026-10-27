#!/usr/bin/env bash
# After `terraform apply`: connect kubectl, install Argo CD and point it at the
# GitOps branch that the GitLab pipeline writes. From then on, Argo CD deploys
# the bank; nobody runs `kubectl apply` for releases any more.
#
#   REPO_URL=https://gitlab.com/<you>/<project>.git ./scripts/gitops-bootstrap.sh
#
# Optional:
#   ARGOCD_UI_IP=<your-ip>   also expose the Argo CD web UI on a load balancer
#                            that only accepts that address
#   GITOPS_REMOTE=gitlab     name of the git remote that holds the gitops branch
#                            (lets sim.sh reset scenarios to exactly what Argo CD deployed)
source "$(dirname "$0")/lib.sh"

ARGOCD_VERSION="v3.5.5"
TF_DIR="$ROOT/terraform"
need terraform "https://developer.hashicorp.com/terraform/install"
need aws "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
need kubectl "https://kubernetes.io/docs/tasks/tools/"
need git "install git"

REGION=$(terraform -chdir="$TF_DIR" output -raw region 2>/dev/null) ||
  die "no Terraform outputs in $TF_DIR - run 'make tf-up' first"
CLUSTER=$(terraform -chdir="$TF_DIR" output -raw cluster_name)
KUBE_CONTEXT="eks-$CLUSTER"
export KUBE_CONTEXT

GITOPS_REMOTE="${GITOPS_REMOTE:-gitlab}"
if [ -z "${REPO_URL:-}" ]; then
  REPO_URL=$(git -C "$ROOT" remote get-url "$GITOPS_REMOTE" 2>/dev/null) ||
    die "set REPO_URL=https://gitlab.com/<you>/<project>.git (or add a git remote named '$GITOPS_REMOTE')"
fi
# Argo CD reads the repo anonymously over HTTPS: turn SSH or token URLs into plain HTTPS.
REPO_URL=$(sed -E -e 's#^git@([^:]+):#https://\1/#' -e 's#https://[^@/]+@#https://#' <<<"$REPO_URL")
case "$REPO_URL" in *.git) ;; *) REPO_URL="$REPO_URL.git" ;; esac

info "Connecting kubectl to $CLUSTER ($REGION)"
aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" --alias "$KUBE_CONTEXT" >/dev/null
k get nodes -L topology.kubernetes.io/zone

mkdir -p "$SIM_STATE"
echo "$KUBE_CONTEXT" >"$SIM_STATE/context"
echo "$REGION" >"$SIM_STATE/eks-region"
echo "lite" >"$SIM_STATE/profile"
echo "$GITOPS_REMOTE" >"$SIM_STATE/gitops-remote"

info "Installing Argo CD $ARGOCD_VERSION"
k create namespace argocd --dry-run=client -o yaml | k apply -f - >/dev/null
# server-side apply: the CRDs are too large for the last-applied annotation
k apply -n argocd --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/$ARGOCD_VERSION/manifests/install.yaml" >/dev/null
k wait --for condition=established --timeout=120s \
  crd/applications.argoproj.io crd/appprojects.argoproj.io >/dev/null
for d in argocd-server argocd-repo-server argocd-redis; do
  k -n argocd rollout status "deploy/$d" --timeout=300s >/dev/null
done
k -n argocd rollout status statefulset/argocd-application-controller --timeout=300s >/dev/null

info "Registering the bank-sim project and application ($REPO_URL, branch gitops)"
for f in project application; do
  sed "s#__REPO_URL__#$REPO_URL#g" "$ROOT/gitops/argocd/$f.yaml" | k apply -f - >/dev/null
done

info "Waiting for the first sync (pulls the images, starts ~75 pods; 3-6 minutes)"
for _ in $(seq 72); do
  sync=$(jp '{.status.sync.status}' application bank-sim -n argocd)
  health=$(jp '{.status.health.status}' application bank-sim -n argocd)
  printf '\r    sync=%-10s health=%-12s' "${sync:-?}" "${health:-?}"
  [ "$sync" = Synced ] && [ "$health" = Healthy ] && break
  sleep 5
done
echo
if [ "$sync" != Synced ]; then
  warn "not synced yet. Common causes: the gitops branch does not exist yet (run the GitLab pipeline on main),"
  warn "or the project is private. Check: kubectl -n argocd get application bank-sim -o yaml | grep -A5 conditions"
fi

if git -C "$ROOT" fetch -q "$GITOPS_REMOTE" gitops 2>/dev/null; then
  gitops_pull && ok "local rendered/ matches the gitops branch (used by ./sim.sh reset)"
fi

if [ -n "${ARGOCD_UI_IP:-}" ]; then
  info "Exposing the Argo CD UI to $ARGOCD_UI_IP only"
  k apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Service
metadata:
  name: argocd-server-lb
  namespace: argocd
spec:
  type: LoadBalancer
  loadBalancerSourceRanges: ["$ARGOCD_UI_IP/32"]
  selector:
    app.kubernetes.io/name: argocd-server
  ports:
    - name: https
      port: 443
      targetPort: 8080
YAML
  for _ in $(seq 60); do
    host=$(jp '{.status.loadBalancer.ingress[0].hostname}' svc argocd-server-lb -n argocd)
    [ -n "$host" ] && break
    sleep 5
  done
  echo "    Argo CD UI: https://${host:-<pending: kubectl -n argocd get svc argocd-server-lb>}"
  echo "    (self-signed certificate: your browser will warn; DNS can take 2-3 minutes)"
fi

echo
info "Argo CD is deploying the bank from Git."
cat <<MSG
    kubectl -n argocd get application bank-sim
    Argo CD login: admin / $(k -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' 2>/dev/null | base64 -d)
    ./sim.sh status
MSG
