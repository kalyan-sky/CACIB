#!/usr/bin/env bash
# Run the bank simulation on Amazon EKS instead of kind.
#
#   AWS_REGION=ap-southeast-1   region (default Singapore)
#   CLUSTER=bank-sim            EKS cluster name
#   PROFILE=lite|full           56 or 216 apps
#   INSTANCE_TYPE=t3.medium     worker size (t3.large suggested for PROFILE=full)
#   K8S_VERSION=                EKS Kubernetes version (default: eksctl's default)
#   PRIVATE_NODES=0|1           1 = nodes in private subnets behind a NAT gateway (bank-like, costs more)
#   NODEGROUP_TYPE=managed|self managed node group (default), or self-managed for accounts whose
#                               policies stop EKS from creating nodes on your behalf
#   NODEGROUP_NAME=             override the node group name, e.g. to add a new one next to a failed one
#   DEPLOY_ONLY=1               skip the cluster: just build/push the image and roll out the apps
#                               (what `make eks-deploy` does after code changes)
#
# This creates billable AWS resources (EKS control plane, EC2, EBS, public IPs,
# optional NAT). Tear everything down with scripts/eks-down.sh when you finish.
source "$(dirname "$0")/lib.sh"

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-ap-southeast-1}}"
PROFILE="${PROFILE:-lite}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.medium}"
PRIVATE_NODES="${PRIVATE_NODES:-0}"
NODEGROUP_TYPE="${NODEGROUP_TYPE:-managed}"
case "$NODEGROUP_TYPE" in
  managed) ng_key=managedNodeGroups ng_name=workers ;;
  self) ng_key=nodeGroups ng_name=workers-self ;;
  *) die "NODEGROUP_TYPE must be managed or self" ;;
esac
ng_name="${NODEGROUP_NAME:-$ng_name}"
REPO="bank-sim/txn-service"
TAG="1.5.0"
KUBE_CONTEXT="eks-$CLUSTER"
export KUBE_CONTEXT

need aws     "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
need eksctl  "https://eksctl.io/installation/"
need kubectl "https://kubernetes.io/docs/tasks/tools/"
need "$PY"   "install Python 3.9+"
"$PY" -c 'import yaml' 2>/dev/null || die "PyYAML missing - run: pip install -r requirements.txt"

# Build the image with Docker if there is one; otherwise (e.g. AWS CloudShell,
# which has no Docker) compile with Go and push with crane - no daemon needed.
if docker info >/dev/null 2>&1; then
  BUILDER=docker
elif command -v go >/dev/null 2>&1; then
  BUILDER=crane
else
  die "need Docker, or Go to build without Docker (CloudShell / Amazon Linux: sudo dnf install -y golang)"
fi

ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) ||
  die "AWS credentials not configured - run 'aws configure' or 'aws sso login'"
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
IMAGE="$REGISTRY/$REPO:$TAG"
export IMAGE

mkdir -p "$SIM_STATE"
echo "$REGION" >"$SIM_STATE/eks-region"   # so eks-down.sh finds it even if this run fails

info "AWS account $ACCOUNT, region $REGION, cluster '$CLUSTER', profile $PROFILE, $INSTANCE_TYPE x3"
warn "this creates billable AWS resources - run scripts/eks-down.sh when you are done"

# Same image as apps/txn-service/Dockerfile (static binary on an empty base,
# UID 10001), assembled with crane instead of docker build.
push_without_docker() {
  local crane work
  crane=$(command -v crane || true)
  if [ -z "$crane" ]; then
    info "Installing crane (container registry tool) into .sim-state/bin"
    GOBIN="$SIM_STATE/bin" go install github.com/google/go-containerregistry/cmd/crane@v0.20.2
    crane="$SIM_STATE/bin/crane"
  fi
  work=$(mktemp -d)
  (cd "$ROOT/apps/txn-service" && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags="-s -w -X main.version=$TAG" -o "$work/txn-service" .)
  "$PY" - "$work" <<'PY'
import os, sys, tarfile
work = sys.argv[1]
with tarfile.open(os.path.join(work, "layer.tar"), "w") as tar:
    info = tar.gettarinfo(os.path.join(work, "txn-service"), arcname="txn-service")
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mode = 0o755
    with open(os.path.join(work, "txn-service"), "rb") as f:
        tar.addfile(info, f)
PY
  aws ecr get-login-password --region "$REGION" | "$crane" auth login "$REGISTRY" -u AWS --password-stdin >/dev/null
  "$crane" append --new_layer "$work/layer.tar" --new_tag "$IMAGE" >/dev/null
  "$crane" mutate "$IMAGE" --set-platform linux/amd64 --entrypoint /txn-service \
    --user 10001:10001 --exposed-ports 8080 --tag "$IMAGE" >/dev/null
  # :latest exists only so scenario 10 can show why unpinned tags are an audit finding
  "$crane" tag "$IMAGE" latest
  rm -rf "$work"
}

if [ "${DEPLOY_ONLY:-0}" = 1 ]; then
  eksctl get cluster --name "$CLUSTER" --region "$REGION" >/dev/null 2>&1 ||
    die "cluster '$CLUSTER' not found in $REGION - run 'make eks-up' first"
  aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" --alias "$KUBE_CONTEXT" >/dev/null
else
  # AWS accounts on the Free plan can only launch free-tier-eligible instance
  # types; anything else fails silently inside the Auto Scaling group.
  free_types=$(aws ec2 describe-instance-types --region "$REGION" --filters Name=free-tier-eligible,Values=true \
    --query 'InstanceTypes[].InstanceType' --output text 2>/dev/null | tr '\t' ' ' || true)
  if [ -n "$free_types" ] && ! grep -qw -- "$INSTANCE_TYPE" <<<"$free_types"; then
    warn "$INSTANCE_TYPE is not free-tier eligible. If this account is on the AWS Free plan, nodes will never start."
    warn "free-tier eligible here: $free_types  (e.g. INSTANCE_TYPE=m7i-flex.large)"
  fi

  # ---------------------------------------------------------------- cluster
  if [ "$PRIVATE_NODES" = 1 ]; then nat=Single private=true; else nat=Disable private=false; fi
  cfg="$SIM_STATE/eks-cluster.yaml"
  sed -e "s|__CLUSTER__|$CLUSTER|g" -e "s|__REGION__|$REGION|g" \
    -e "s|__INSTANCE_TYPE__|$INSTANCE_TYPE|g" -e "s|__NAT__|$nat|g" -e "s|__PRIVATE__|$private|g" \
    -e "s|__NODEGROUPS__|$ng_key|g" -e "s|__NODEGROUP_NAME__|$ng_name|g" \
    "$ROOT/platform/eks-cluster.yaml" >"$cfg"
  if [ -n "${K8S_VERSION:-}" ]; then
    sed -i.bak "s|__K8S_VERSION__|$K8S_VERSION|" "$cfg"
  else
    sed -i.bak '/__K8S_VERSION__/d' "$cfg"
  fi

  if eksctl get cluster --name "$CLUSTER" --region "$REGION" >/dev/null 2>&1; then
    info "EKS cluster '$CLUSTER' already exists - reusing it"
    aws eks wait cluster-active --name "$CLUSTER" --region "$REGION"
    # A previous run may have been interrupted (e.g. a CloudShell session timing
    # out) after the control plane but before the worker nodes: finish the job.
    if ! eksctl get nodegroup --cluster "$CLUSTER" --region "$REGION" -o json 2>/dev/null | grep -q "\"$ng_name\""; then
      # vpc-cni must carry the network-policy / prefix-delegation config before nodes join
      eksctl get addon --cluster "$CLUSTER" --region "$REGION" -o json 2>/dev/null | grep -q '"vpc-cni"' ||
        die "cluster '$CLUSTER' was only partly created (no add-ons). Delete it with 'make eks-down', then run 'make eks-up' again"
      info "Node group '$ng_name' missing (first run interrupted, or switching NODEGROUP_TYPE) - creating it"
      eksctl create nodegroup -f "$cfg"
    fi

  else
    info "Creating EKS cluster (15-20 minutes)"
    eksctl create cluster -f "$cfg"
  fi
  aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" --alias "$KUBE_CONTEXT" >/dev/null

  info "Checking pod capacity and NetworkPolicy support"
  max_pods=$(jp '{.items[0].status.allocatable.pods}' nodes)
  if [ "${max_pods:-0}" -lt 100 ]; then
    warn "nodes allow only $max_pods pods each - prefix delegation did not apply to them."
    warn "fix: eksctl upgrade nodegroup / replace the node group after the vpc-cni addon is configured"
  fi
  # The agent container is aws-eks-nodeagent (older releases: aws-network-policy-agent);
  # it only enforces policies when started with --enable-network-policy=true.
  k -n kube-system get ds aws-node -o jsonpath='{range .spec.template.spec.containers[*]}{.name} {.args}{"\n"}{end}' |
    grep -E 'aws-eks-nodeagent|network-policy-agent' | grep -q 'enable-network-policy=true' ||
    warn "VPC CNI network policy enforcement not enabled - scenario 03 will not reproduce (check the vpc-cni addon config)"
fi

# ---------------------------------------------------------------- image
info "Building and pushing $IMAGE (linux/amd64, with $BUILDER) to ECR"
aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" >/dev/null 2>&1 ||
  aws ecr create-repository --repository-name "$REPO" --region "$REGION" \
    --image-scanning-configuration scanOnPush=true >/dev/null
if [ "$BUILDER" = docker ]; then
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
  # :latest exists only so scenario 10 can show why unpinned tags are an audit finding
  docker buildx build --platform linux/amd64 --push -q \
    -t "$IMAGE" -t "$REGISTRY/$REPO:latest" "$ROOT/apps/txn-service" >/dev/null
else
  push_without_docker
fi

# ---------------------------------------------------------------- bank
PROFILE="$PROFILE" "$ROOT/scripts/deploy.sh"
echo "$REGION" >"$SIM_STATE/eks-region"
