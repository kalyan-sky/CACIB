#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
k -n trade-finance annotate deploy lc-issuance --overwrite \
  "kubernetes.io/change-cause=CD pipeline: release lc-issuance 1.4.1"
k -n trade-finance set image deploy/lc-issuance app=bank-sim/txn-service:1.4.1-hotfx
