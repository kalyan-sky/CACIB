#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
k -n payments patch configmap payments-gateway-config --type=json \
  -p='[{"op":"remove","path":"/data/DB_URL"}]' 2>/dev/null || true
# "overnight node maintenance" recycles the pods - they now read the broken config
k -n payments delete pod -l app.kubernetes.io/name=payments-gateway --wait=false
