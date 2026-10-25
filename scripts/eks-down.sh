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

info "Deleting EKS cluster '$CLUSTER' in $REGION (10-15 minutes)"
if ! eksctl delete cluster --name "$CLUSTER" --region "$REGION" --wait; then
  # eksctl refuses when it cannot check IAM/OIDC resources (e.g. denied by a
  # service control policy). Everything it created lives in CloudFormation
  # stacks, so delete those directly: node groups / add-ons first, cluster last.
  warn "eksctl could not delete the cluster - deleting its CloudFormation stacks directly"
  stacks=$(aws cloudformation list-stacks --region "$REGION" \
    --stack-status-filter CREATE_COMPLETE CREATE_FAILED ROLLBACK_COMPLETE ROLLBACK_FAILED UPDATE_COMPLETE \
    UPDATE_ROLLBACK_COMPLETE DELETE_FAILED \
    --query "StackSummaries[?starts_with(StackName, 'eksctl-$CLUSTER-')].StackName" --output text)
  [ -n "$stacks" ] || die "no eksctl-$CLUSTER-* stacks found in $REGION - check the CloudFormation console"
  for stack in $stacks; do
    [ "$stack" = "eksctl-$CLUSTER-cluster" ] && continue
    info "  deleting stack $stack"
    aws cloudformation delete-stack --stack-name "$stack" --region "$REGION"
    aws cloudformation wait stack-delete-complete --stack-name "$stack" --region "$REGION"
  done
  if grep -qw "eksctl-$CLUSTER-cluster" <<<"$stacks"; then
    info "  deleting stack eksctl-$CLUSTER-cluster (control plane, VPC, IAM roles)"
    aws cloudformation delete-stack --stack-name "eksctl-$CLUSTER-cluster" --region "$REGION"
    aws cloudformation wait stack-delete-complete --stack-name "eksctl-$CLUSTER-cluster" --region "$REGION"
  fi
fi
info "Deleting ECR repository $REPO"
aws ecr delete-repository --repository-name "$REPO" --region "$REGION" --force >/dev/null 2>&1 || true
kubectl config delete-context "eks-$CLUSTER" >/dev/null 2>&1 || true
rm -rf "$SIM_STATE"
info "Done. Check the EC2 / EKS / CloudFormation consoles in $REGION if you want to be sure nothing is left."
