#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
k -n core-banking patch svc account-service --type=merge \
  -p '{"metadata":{"annotations":{"bank.sim/change":"PR #482 rename labels to account-svc"}},"spec":{"selector":{"app.kubernetes.io/name":"account-svc"}}}'
