#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
# the team's manifest, pointed at the image this cluster already runs (kind, ECR or GitLab)
img=$(jp '{.spec.template.spec.containers[0].image}' deploy lc-issuance -n trade-finance)
sed "s|image: bank-sim/txn-service:1.5.0|image: ${img:-$IMAGE}|" "$(dirname "$0")/trade-docs-ocr.yaml" | k apply -f -
