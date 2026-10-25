#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
k -n trade-finance annotate deploy lc-issuance --overwrite \
  "kubernetes.io/change-cause=CD pipeline: release lc-issuance 1.4.1"
# same repository as the running image (local or ECR), with a typo in the tag
repo=$(jp '{.spec.template.spec.containers[0].image}' deploy lc-issuance -n trade-finance)
k -n trade-finance set image deploy/lc-issuance "app=${repo%:*}:1.4.1-hotfx"
