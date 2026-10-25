#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
k -n treasury-markets annotate deploy risk-calc-engine --overwrite \
  "kubernetes.io/change-cause=release v2.3.1: enable eager VaR cache"
k -n treasury-markets set env deploy/risk-calc-engine LEAK_MB_PER_SEC=4 CACHE_MODE=eager
