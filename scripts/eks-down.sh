#!/usr/bin/env bash
# Delete everything eks-up.sh created: the EKS cluster (with its VPC, nodes,
# volumes) and the ECR repository. Set YES=1 to skip the confirmation.
source "$(dirname "$0")/lib.sh"

REGION="${AWS_REGION:-$(cat "$SIM_STATE/eks-region" 2>/dev/null || echo ap-southeast-1)}"
REPO="bank-sim/txn-service"
need aws "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
need eksctl "https://eksctl.io/installation/"

if [ "${YES:-0}" != 1 ]; then
  printf 'Delete EKS cluster "%s" and ECR repository "%s" in %s? Type the cluster name to confirm: ' \
    "$CLUSTER" "$REPO" "$REGION"
  read -r answer
  [ "$answer" = "$CLUSTER" ] || die "aborted"
fi

info "Deleting EKS cluster '$CLUSTER' (10-15 minutes)"
eksctl delete cluster --name "$CLUSTER" --region "$REGION" --wait
info "Deleting ECR repository $REPO"
aws ecr delete-repository --repository-name "$REPO" --region "$REGION" --force >/dev/null 2>&1 || true
kubectl config delete-context "eks-$CLUSTER" >/dev/null 2>&1 || true
rm -rf "$SIM_STATE"
info "Done. Check the EC2 / EKS / VPC consoles in $REGION if you want to be sure nothing is left."
