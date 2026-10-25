#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
k -n shared-services annotate deploy notification-service --overwrite \
  "kubernetes.io/change-cause=perf tuning: readiness on admin port 8081, Recreate strategy to save capacity"
k -n shared-services patch deploy notification-service --type=json -p='[
  {"op":"replace","path":"/spec/strategy","value":{"type":"Recreate"}},
  {"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/port","value":8081}]'
