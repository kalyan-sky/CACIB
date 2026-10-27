#!/usr/bin/env bash
# Delete the Terraform-built environment, in an order that lets the VPC go:
#   1. the portal's Lambda (its network interfaces sit in the VPC)
#   2. load balancers created by Kubernetes Services (not in Terraform state)
#   3. terraform destroy
# Set YES=1 to skip the confirmation.
source "$(dirname "$0")/lib.sh"

TF_DIR="$ROOT/terraform"
need terraform "https://developer.hashicorp.com/terraform/install"
need aws "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"

REGION=$(tf_output region)
CLUSTER=$(tf_output cluster_name)
KUBE_CONTEXT="eks-$CLUSTER"
export KUBE_CONTEXT AWS_REGION="$REGION"

if [ "${YES:-0}" != 1 ]; then
  printf 'Destroy EKS cluster "%s", its VPC and everything in it (%s)? Type the cluster name to confirm: ' "$CLUSTER" "$REGION"
  read -r answer
  [ "$answer" = "$CLUSTER" ] || die "aborted"
fi

if aws lambda get-function --function-name "$CLUSTER-portal" >/dev/null 2>&1 ||
  aws iam get-role --role-name "$CLUSTER-portal-lambda" >/dev/null 2>&1; then
  CLUSTER="$CLUSTER" "$ROOT/scripts/portal-lambda.sh" down
fi

if k get --raw /readyz >/dev/null 2>&1; then
  info "Deleting Kubernetes Services of type LoadBalancer (their AWS load balancers are not in Terraform)"
  lbs=$(k get svc -A -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}')
  for svc in $lbs; do
    echo "    $svc"
    k -n "${svc%%/*}" delete svc "${svc#*/}" --wait=true >/dev/null
  done
  if [ -n "$lbs" ]; then
    info "Waiting for AWS to remove those load balancers"
    vpc=$(tf_output vpc_id)
    for _ in $(seq 40); do
      left=$(aws elb describe-load-balancers --query "length(LoadBalancerDescriptions[?VPCId=='$vpc'])" --output text 2>/dev/null || echo 0)
      left2=$(aws elbv2 describe-load-balancers --query "length(LoadBalancers[?VpcId=='$vpc'])" --output text 2>/dev/null || echo 0)
      [ "${left:-0}" = 0 ] && [ "${left2:-0}" = 0 ] && break
      sleep 15
    done
  fi
fi

info "terraform destroy (10-15 minutes)"
terraform -chdir="$TF_DIR" destroy -auto-approve -input=false

kubectl config delete-context "$KUBE_CONTEXT" >/dev/null 2>&1 || true
rm -f "$SIM_STATE/context" "$SIM_STATE/eks-region" "$SIM_STATE/gitops-remote"
info "Done. The GitLab project and its container registry are untouched (free); delete them in GitLab if you want."
