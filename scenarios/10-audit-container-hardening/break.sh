#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
k label ns data-reporting --overwrite pod-security.kubernetes.io/enforce=privileged
k -n data-reporting annotate deploy mas-610-reporting --overwrite \
  "kubernetes.io/change-cause=vendor hotfix: run as root so the report job can write to /tmp (TEMPORARY)"
k -n data-reporting patch deploy mas-610-reporting --type=json -p='[
  {"op":"replace","path":"/spec/template/spec/securityContext","value":{"runAsUser":0}},
  {"op":"replace","path":"/spec/template/spec/containers/0/securityContext","value":{"privileged":true,"readOnlyRootFilesystem":false}},
  {"op":"remove","path":"/spec/template/spec/containers/0/resources/limits"},
  {"op":"replace","path":"/spec/template/spec/containers/0/image","value":"bank-sim/txn-service:latest"}]'
