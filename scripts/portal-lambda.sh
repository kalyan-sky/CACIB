#!/usr/bin/env bash
# Serve the bank portal from AWS Lambda (EKS only).
#
#   browser --HTTPS--> Lambda function URL --(VPC)--> internal load balancer --> bank-portal in EKS
#
#   ./scripts/portal-lambda.sh up <your-ip>[,<cidr>...]   create / update everything
#   ./scripts/portal-lambda.sh status
#   ./scripts/portal-lambda.sh down                        delete everything this script created
#
# Find <your-ip> by opening https://checkip.amazonaws.com in the browser you will use.
# Only those addresses can open the portal. Lambda, its URL and the internal load
# balancer are billable in the usual way (Lambda is within the always-free tier).
source "$(dirname "$0")/lib.sh"

REGION="${AWS_REGION:-$(cat "$SIM_STATE/eks-region" 2>/dev/null || echo ap-southeast-1)}"
FN="$CLUSTER-portal"
ROLE="$CLUSTER-portal-lambda"
SG_NAME="$CLUSTER-portal-lambda"
LB_SVC="bank-portal-internal"
NS="sim-traffic"
need aws "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
export AWS_REGION="$REGION" AWS_PAGER=""

cluster_vpc() {
  aws eks describe-cluster --name "$CLUSTER" --query cluster.resourcesVpcConfig.vpcId --output text
}

find_sg() {
  aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$1" "Name=group-name,Values=$SG_NAME" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null
}

normalise_cidrs() { # "1.2.3.4, 10.0.0.0/8" -> "1.2.3.4/32,10.0.0.0/8"; refuses the whole internet
  "$PY" - "$1" <<'PY'
import ipaddress, os, sys
out = []
for item in sys.argv[1].replace(" ", "").split(","):
    if not item:
        continue
    try:
        net = ipaddress.ip_network(item, strict=False)
    except ValueError:
        sys.exit(f"not an IP address or CIDR: {item}")
    if net.prefixlen == 0 and os.environ.get("PUBLIC") != "1":
        sys.exit("refusing to open the portal to the whole internet (set PUBLIC=1 if you really mean it)")
    out.append(str(net))
if not out:
    sys.exit("no addresses given")
print(",".join(out))
PY
}

cmd_up() {
  local cidrs vpc vpc_cidr host subnets sg role_arn zip url env_json payload result
  cidrs=$(normalise_cidrs "${1:-${ALLOWED_CIDRS:-}}") ||
    die "usage: $0 up <your-ip>  (open https://checkip.amazonaws.com in your browser to find it)"
  k -n "$NS" get deploy bank-portal >/dev/null 2>&1 ||
    die "bank-portal is not deployed in '$KUBE_CONTEXT' - run 'make eks-deploy' first"

  vpc=$(cluster_vpc) || die "EKS cluster '$CLUSTER' not found in $REGION"
  vpc_cidr=$(aws ec2 describe-vpcs --vpc-ids "$vpc" --query 'Vpcs[0].CidrBlock' --output text)

  info "1/6 Internal load balancer in front of bank-portal (reachable only inside $vpc_cidr)"
  k apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Service
metadata:
  name: $LB_SVC
  namespace: $NS
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-internal: "true"
spec:
  type: LoadBalancer
  loadBalancerSourceRanges: ["$vpc_cidr"]
  selector:
    app.kubernetes.io/name: bank-portal
  ports:
    - name: http
      port: 80
      targetPort: http
YAML
  for _ in $(seq 60); do
    host=$(jp '{.status.loadBalancer.ingress[0].hostname}' svc "$LB_SVC" -n "$NS")
    [ -n "$host" ] && break
    sleep 5
  done
  [ -n "$host" ] || die "load balancer not provisioned after 5 minutes: kubectl -n $NS describe svc $LB_SVC"
  echo "    $host"

  info "2/6 Lambda networking (private subnets of the cluster VPC + a security group)"
  subnets=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$vpc" "Name=tag:kubernetes.io/role/internal-elb,Values=1" \
    --query 'Subnets[].SubnetId' --output text | tr '\t' ',')
  [ -n "$subnets" ] || subnets=$(aws eks describe-cluster --name "$CLUSTER" \
    --query 'cluster.resourcesVpcConfig.subnetIds' --output text | tr '\t' ',')
  sg=$(find_sg "$vpc" || true)
  if [ -z "$sg" ] || [ "$sg" = None ]; then
    sg=$(aws ec2 create-security-group --group-name "$SG_NAME" --vpc-id "$vpc" \
      --description "bank-sim portal Lambda (egress to the internal load balancer)" --query GroupId --output text)
  fi
  echo "    subnets $subnets, security group $sg"

  info "3/6 IAM role for the function (least privilege: VPC network interfaces + logs)"
  if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
    aws iam create-role --role-name "$ROLE" --description "bank-sim portal Lambda" \
      --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
    aws iam attach-role-policy --role-name "$ROLE" \
      --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole
  fi
  role_arn=$(aws iam get-role --role-name "$ROLE" --query Role.Arn --output text)

  info "4/6 Packaging the UI (apps/txn-service/web) with the handler"
  zip="$SIM_STATE/portal-lambda.zip"
  mkdir -p "$SIM_STATE"
  "$PY" - "$ROOT" "$zip" <<'PY'
import pathlib, sys, zipfile
root, out = pathlib.Path(sys.argv[1]), sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(root / "lambda/portal-edge/handler.py", "handler.py")
    for f in sorted((root / "apps/txn-service/web").rglob("*")):
        if f.is_file():
            z.write(f, "web/" + f.relative_to(root / "apps/txn-service/web").as_posix())
PY
  env_json="{\"Variables\":{\"BACKEND_URL\":\"http://$host\",\"ALLOWED_CIDRS\":\"$cidrs\"}}"

  info "5/6 Deploying function $FN"
  if aws lambda get-function --function-name "$FN" >/dev/null 2>&1; then
    aws lambda update-function-code --function-name "$FN" --zip-file "fileb://$zip" >/dev/null
    aws lambda wait function-updated --function-name "$FN"
    aws lambda update-function-configuration --function-name "$FN" --environment "$env_json" \
      --vpc-config "SubnetIds=$subnets,SecurityGroupIds=$sg" >/dev/null
    aws lambda wait function-updated --function-name "$FN"
  else
    for attempt in 1 2 3 4 5 6; do # a new IAM role takes a few seconds to become usable
      if aws lambda create-function --function-name "$FN" --runtime python3.13 --architectures arm64 \
        --handler handler.handler --role "$role_arn" --zip-file "fileb://$zip" \
        --timeout 15 --memory-size 256 --environment "$env_json" \
        --vpc-config "SubnetIds=$subnets,SecurityGroupIds=$sg" \
        --tags project=bank-sim >/dev/null 2>"$SIM_STATE/lambda-create.err"; then
        break
      fi
      grep -q "cannot be assumed\|InvalidParameterValueException" "$SIM_STATE/lambda-create.err" && [ "$attempt" -lt 6 ] ||
        die "create-function failed: $(cat "$SIM_STATE/lambda-create.err")"
      sleep 10
    done
    echo "    waiting for the function to attach to the VPC (1-2 minutes the first time)"
    aws lambda wait function-active --function-name "$FN"
  fi

  info "6/6 Function URL"
  url=$(aws lambda get-function-url-config --function-name "$FN" --query FunctionUrl --output text 2>/dev/null) ||
    url=$(aws lambda create-function-url-config --function-name "$FN" --auth-type NONE --query FunctionUrl --output text)
  aws lambda add-permission --function-name "$FN" --statement-id public-function-url \
    --action lambda:InvokeFunctionUrl --principal "*" --function-url-auth-type NONE >/dev/null 2>&1 || true
  # Newer accounts also require InvokeFunction, scoped to calls made through the URL.
  aws lambda add-permission --function-name "$FN" --statement-id public-function-url-invoke \
    --action lambda:InvokeFunction --principal "*" --invoked-via-function-url >/dev/null 2>&1 || true
  echo "$url" >"$SIM_STATE/portal-url"

  info "Smoke test: invoking the function as $(cut -d, -f1 <<<"$cidrs" | cut -d/ -f1) -> /api/meta"
  payload=$(printf '{"rawPath":"/api/meta","rawQueryString":"","headers":{},"requestContext":{"http":{"method":"GET","sourceIp":"%s"}}}' \
    "$(cut -d, -f1 <<<"$cidrs" | cut -d/ -f1)")
  aws lambda invoke --function-name "$FN" --cli-binary-format raw-in-base64-out --payload "$payload" \
    "$SIM_STATE/lambda-smoke.json" >/dev/null
  result=$("$PY" -c 'import json,sys; r=json.load(open(sys.argv[1])); print(r.get("statusCode"), r.get("errorMessage",""))' \
    "$SIM_STATE/lambda-smoke.json")
  if [ "${result%% *}" = 200 ]; then
    ok "Lambda reached bank-portal in EKS through the internal load balancer"
  else
    warn "smoke test returned: $result (see: aws logs tail /aws/lambda/$FN --since 10m)"
  fi
  echo
  info "Bank portal: $url"
  echo "    allowed from: $cidrs   (re-run 'up' with a new IP if yours changes)"
}

cmd_status() {
  local url
  url=$(aws lambda get-function-url-config --function-name "$FN" --query FunctionUrl --output text 2>/dev/null) ||
    { echo "function $FN not deployed in $REGION"; return 0; }
  echo "URL:      $url"
  echo "Function: $(aws lambda get-function --function-name "$FN" --query 'Configuration.[State,LastUpdateStatus,Runtime]' --output text)"
  echo "Allowed:  $(aws lambda get-function-configuration --function-name "$FN" --query 'Environment.Variables.ALLOWED_CIDRS' --output text)"
  echo "Backend:  $(aws lambda get-function-configuration --function-name "$FN" --query 'Environment.Variables.BACKEND_URL' --output text)"
}

cmd_down() {
  local vpc sg
  info "Deleting function URL and function $FN"
  aws lambda delete-function-url-config --function-name "$FN" >/dev/null 2>&1 || true
  aws lambda delete-function --function-name "$FN" >/dev/null 2>&1 || true
  if k get --raw /readyz >/dev/null 2>&1; then
    info "Deleting internal load balancer service"
    k -n "$NS" delete svc "$LB_SVC" --ignore-not-found >/dev/null
  fi
  if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
    info "Deleting IAM role $ROLE"
    aws iam detach-role-policy --role-name "$ROLE" \
      --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole 2>/dev/null || true
    aws iam delete-role --role-name "$ROLE"
  fi
  vpc=$(cluster_vpc 2>/dev/null) || vpc=""
  sg=""
  [ -n "$vpc" ] && sg=$(find_sg "$vpc" || true)
  if [ -n "$sg" ] && [ "$sg" != None ]; then
    # Lambda releases its VPC network interfaces asynchronously (up to ~20 minutes);
    # the security group (and later the VPC) cannot be deleted until then.
    info "Deleting security group $sg (waits for Lambda to release its network interfaces, up to ~20 min)"
    for _ in $(seq 50); do
      aws ec2 delete-security-group --group-id "$sg" 2>/dev/null && { ok "security group deleted"; break; }
      sleep 30
    done
  fi
  rm -f "$SIM_STATE/portal-url" "$SIM_STATE/portal-lambda.zip"
  info "Lambda front door removed"
}

case "${1:-}" in
  up) cmd_up "${2:-}" ;;
  status) cmd_status ;;
  down) cmd_down ;;
  *) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 1 ;;
esac
