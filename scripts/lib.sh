# Shared helpers for setup, sim.sh and scenario scripts. Source, don't execute.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIM_STATE="$ROOT/.sim-state"
CLUSTER="${CLUSTER:-bank-sim}"
# setup.sh (kind) and eks-up.sh record which cluster and image this checkout
# drives; environment variables override them.
KUBE_CONTEXT="${KUBE_CONTEXT:-$(cat "$SIM_STATE/context" 2>/dev/null || echo "kind-$CLUSTER")}"
IMAGE="${IMAGE:-$(cat "$SIM_STATE/image" 2>/dev/null || echo bank-sim/txn-service:1.5.0)}"
export KUBE_CONTEXT IMAGE
PY="${PYTHON:-python3}"
RENDERED="$ROOT/rendered"
FAILED=0

if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'; C_BLD=$'\033[1m'; C_DIM=$'\033[2m'; C_RST=$'\033[0m'
else
  C_RED=; C_GRN=; C_YLW=; C_BLD=; C_DIM=; C_RST=
fi

k() { kubectl --context "$KUBE_CONTEXT" "$@"; }
info() { printf '%s==>%s %s\n' "$C_BLD" "$C_RST" "$*"; }
warn() { printf '%swarning:%s %s\n' "$C_YLW" "$C_RST" "$*" >&2; }
die() { printf '%serror:%s %s\n' "$C_RED" "$C_RST" "$*" >&2; exit 1; }
ok() { printf '  %s✔%s %s\n' "$C_GRN" "$C_RST" "$*"; }
ko() { printf '  %s✘%s %s\n' "$C_RED" "$C_RST" "$*"; FAILED=1; }
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' not found - $2"; }

# jp <jsonpath> <kubectl get args...>   - print a field, empty string if missing
jp() { local path="$1"; shift; k get "$@" -o "jsonpath=$path" 2>/dev/null || true; }

# deploy_healthy <ns> <name>: every desired replica is updated and available
deploy_healthy() {
  local want upd avail
  want=$(jp '{.spec.replicas}' deploy "$2" -n "$1")
  upd=$(jp '{.status.updatedReplicas}' deploy "$2" -n "$1")
  avail=$(jp '{.status.availableReplicas}' deploy "$2" -n "$1")
  [ -n "$want" ] && [ "$want" -gt 0 ] && [ "${upd:-0}" = "$want" ] && [ "${avail:-0}" -ge "$want" ] &&
    [ -z "$(jp '{.status.unavailableReplicas}' deploy "$2" -n "$1")" ]
}

# probe_from <ns> <deploy> <url> [container]: HTTP GET from inside a running pod
# of <deploy>; prints status line and body, exit status 0 on 2xx/3xx.
probe_from() { k exec -n "$1" "deploy/$2" -c "${4:-app}" -- /txn-service probe "$3" 2>&1; }

# edge_txn <ns> <app>: an end-to-end transaction from the traffic tier, as a customer would
edge_txn() { probe_from sim-traffic traffic-simulator "$(svc "$1" "$2")/transaction" loadgen; }

svc() { echo "http://$2.$1.svc.cluster.local:8080"; }

# restore <ns>/<app> ... : put an app back exactly as rendered from the inventory
restore() { "$PY" "$ROOT/tools/platform.py" restore "$@"; }

check_result() {
  echo
  if [ "$FAILED" = 0 ]; then
    printf '%s%sPASS%s - all acceptance criteria met.\n' "$C_BLD" "$C_GRN" "$C_RST"
  else
    printf '%s%sNOT YET%s - keep going (hint: ./sim.sh hint <id>, or ./k8s-classics.sh hint <id> for the classics).\n' "$C_BLD" "$C_RED" "$C_RST"
  fi
  return "$FAILED"
}

# tf_output <name>: read a Terraform output from terraform/. Re-runs `terraform
# init` first when needed (e.g. a new CloudShell session wiped TF_DATA_DIR in
# /tmp), and shows Terraform's own error instead of hiding it.
tf_output() {
  local dir="$ROOT/terraform" out
  [ -f "$dir/terraform.tfstate" ] ||
    die "no terraform.tfstate in $dir - has 'terraform apply' run from this folder? (never delete that file)"
  if ! out=$(terraform -chdir="$dir" output -raw "$1" 2>&1); then
    terraform -chdir="$dir" init -input=false >/dev/null 2>&1 || true
    out=$(terraform -chdir="$dir" output -raw "$1" 2>&1) || die "terraform output $1 failed: $out"
  fi
  printf '%s' "$out"
}

# gitops_pull: in GitOps mode (scripts/gitops-bootstrap.sh), make rendered/ an
# exact copy of what Argo CD deploys - the gitops branch - so scenario resets
# restore the same image tags and settings that are running.
gitops_pull() {
  local remote tmp
  remote=$(cat "$SIM_STATE/gitops-remote" 2>/dev/null) || return 0
  git -C "$ROOT" fetch -q "$remote" gitops 2>/dev/null ||
    { warn "could not fetch $remote/gitops - using the existing rendered/"; return 0; }
  tmp=$(mktemp -d)
  git -C "$ROOT" archive "$remote/gitops" deploy/eks | tar -x -C "$tmp"
  rm -rf "$RENDERED"
  mv "$tmp/deploy/eks" "$RENDERED"
  rm -rf "$tmp"
}

require_cluster() {
  k get --raw /readyz >/dev/null 2>&1 ||
    die "cluster context '$KUBE_CONTEXT' not reachable - run 'make up' (kind), 'make eks-up' or 'make tf-up' first"
  if [ -f "$SIM_STATE/gitops-remote" ]; then
    gitops_pull
  fi
  [ -d "$RENDERED" ] || die "rendered/ missing - run 'make render apply'"
}
