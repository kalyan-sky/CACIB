#!/usr/bin/env bash
# Create the local "private cloud": kind cluster, zone labels and the shared
# application image, then deploy every application (scripts/deploy.sh).
# For AWS instead of a laptop, see scripts/eks-up.sh.
#
#   PROFILE=lite|full   how many of the 216 apps to deploy (default lite, ~56 apps)
#   CNI=kindnet|calico  kindnet (kind >= 0.25) enforces NetworkPolicy natively
source "$(dirname "$0")/lib.sh"

PROFILE="${PROFILE:-lite}"
CNI="${CNI:-kindnet}"
# Always target the kind cluster, even if this checkout last drove EKS.
KUBE_CONTEXT="kind-$CLUSTER"
IMAGE="bank-sim/txn-service:1.4.0"
export KUBE_CONTEXT IMAGE

need docker  "install Docker Desktop / Docker Engine"
need kind    "https://kind.sigs.k8s.io/docs/user/quick-start/#installation"
need kubectl "https://kubernetes.io/docs/tasks/tools/"
need "$PY"   "install Python 3.9+"
"$PY" -c 'import yaml' 2>/dev/null || die "PyYAML missing - run: pip install -r requirements.txt"
docker info >/dev/null 2>&1 || die "Docker daemon is not running"

kind_ver=$(kind version | sed -E 's/^kind v([0-9]+)\.([0-9]+).*/\1 \2/')
read -r kmaj kmin <<<"$kind_ver"
if [ "$CNI" = kindnet ] && [ "$kmaj" -eq 0 ] && [ "$kmin" -lt 25 ]; then
  warn "kind < v0.25 does not enforce NetworkPolicy; upgrade kind or run with CNI=calico"
fi
if [ "$PROFILE" = full ]; then
  warn "full profile runs ~260 pods: give Docker at least 8 GB RAM / 4 CPUs"
fi

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  info "kind cluster '$CLUSTER' already exists - reusing it"
else
  info "Creating kind cluster '$CLUSTER' (1 control plane, 3 workers, CNI=$CNI)"
  cfg="$ROOT/platform/kind-cluster.yaml"
  if [ "$CNI" = calico ]; then
    cfg="$(mktemp)"
    cat "$ROOT/platform/kind-cluster.yaml" >"$cfg"
    printf 'networking:\n  disableDefaultCNI: true\n  podSubnet: 192.168.0.0/16\n' >>"$cfg"
  fi
  kind create cluster --config "$cfg" --wait 120s
  if [ "$CNI" = calico ]; then
    info "Installing Calico"
    k apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.2/manifests/calico.yaml
    k -n kube-system rollout status ds/calico-node --timeout=300s
  fi
fi

info "Labelling workers as availability zones across two Singapore data centres"
zones=(sg-dc1-a sg-dc1-b sg-dc2-a)
i=0
for node in $(k get nodes -l '!node-role.kubernetes.io/control-plane' -o name | sort); do
  z=${zones[$((i % 3))]}
  k label "$node" --overwrite "topology.kubernetes.io/zone=$z" "bank.sim/datacentre=${z%-*}" >/dev/null
  echo "  ${node#node/} -> $z"
  i=$((i + 1))
done

info "Building application image $IMAGE"
docker build -q -t "$IMAGE" -t "${IMAGE%:*}:latest" "$ROOT/apps/txn-service" >/dev/null
kind load docker-image --name "$CLUSTER" "$IMAGE" "${IMAGE%:*}:latest" >/dev/null

PROFILE="$PROFILE" "$ROOT/scripts/deploy.sh"
