#!/usr/bin/env bash
# Run the bank simulation on Amazon EKS instead of kind.
#
#   AWS_REGION=ap-southeast-1   region (default Singapore)
#   CLUSTER=bank-sim            EKS cluster name
#   PROFILE=lite|full           56 or 216 apps
#   INSTANCE_TYPE=t3.medium     worker size (t3.large suggested for PROFILE=full)
#   K8S_VERSION=                EKS Kubernetes version (default: eksctl's default)
#   PRIVATE_NODES=0|1           1 = nodes in private subnets behind a NAT gateway (bank-like, costs more)
#
# This creates billable AWS resources (EKS control plane, EC2, EBS, public IPs,
# optional NAT). Tear everything down with scripts/eks-down.sh when you finish.
source "$(dirname "$0")/lib.sh"

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-ap-southeast-1}}"
PROFILE="${PROFILE:-lite}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.medium}"
PRIVATE_NODES="${PRIVATE_NODES:-0}"
REPO="bank-sim/txn-service"
TAG="1.4.0"
KUBE_CONTEXT="eks-$CLUSTER"
export KUBE_CONTEXT

need aws     "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
need eksctl  "https://eksctl.io/installation/"
need kubectl "https://kubernetes.io/docs/tasks/tools/"
need docker  "Docker with buildx, to build and push the image"
need "$PY"   "install Python 3.9+"
"$PY" -c 'import yaml' 2>/dev/null || die "PyYAML missing - run: pip install -r requirements.txt"
docker info >/dev/null 2>&1 || die "Docker daemon is not running"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) ||
  die "AWS credentials not configured - run 'aws configure' or 'aws sso login'"
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
IMAGE="$REGISTRY/$REPO:$TAG"
export IMAGE

info "AWS account $ACCOUNT, region $REGION, cluster '$CLUSTER', profile $PROFILE, $INSTANCE_TYPE x3"
warn "this creates billable AWS resources - run scripts/eks-down.sh when you are done"

# ---------------------------------------------------------------- cluster
if eksctl get cluster --name "$CLUSTER" --region "$REGION" >/dev/null 2>&1; then
  info "EKS cluster '$CLUSTER' already exists - reusing it"
else
  if [ "$PRIVATE_NODES" = 1 ]; then nat=Single private=true; else nat=Disable private=false; fi
  cfg="$(mktemp)"
  sed -e "s|__CLUSTER__|$CLUSTER|g" -e "s|__REGION__|$REGION|g" \
    -e "s|__INSTANCE_TYPE__|$INSTANCE_TYPE|g" -e "s|__NAT__|$nat|g" -e "s|__PRIVATE__|$private|g" \
    "$ROOT/platform/eks-cluster.yaml" >"$cfg"
  if [ -n "${K8S_VERSION:-}" ]; then
    sed -i.bak "s|__K8S_VERSION__|$K8S_VERSION|" "$cfg"
  else
    sed -i.bak '/__K8S_VERSION__/d' "$cfg"
  fi
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
k -n kube-system get ds aws-node -o jsonpath='{.spec.template.spec.containers[*].name}' | grep -q network-policy-agent ||
  warn "VPC CNI network policy agent not found - scenario 03 will not reproduce (check the vpc-cni addon config)"

# ---------------------------------------------------------------- image
info "Building and pushing $IMAGE (linux/amd64) to ECR"
aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" >/dev/null 2>&1 ||
  aws ecr create-repository --repository-name "$REPO" --region "$REGION" \
    --image-scanning-configuration scanOnPush=true >/dev/null
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
# :latest exists only so scenario 10 can show why unpinned tags are an audit finding
docker buildx build --platform linux/amd64 --push -q \
  -t "$IMAGE" -t "$REGISTRY/$REPO:latest" "$ROOT/apps/txn-service" >/dev/null

# ---------------------------------------------------------------- bank
PROFILE="$PROFILE" "$ROOT/scripts/deploy.sh"
echo "$REGION" >"$SIM_STATE/eks-region"
