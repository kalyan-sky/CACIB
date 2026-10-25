#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
restore core-banking/account-service
k -n core-banking annotate svc account-service bank.sim/change- >/dev/null 2>&1 || true
