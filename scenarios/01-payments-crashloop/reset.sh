#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
restore payments/payments-gateway
k -n payments rollout restart deploy/payments-gateway >/dev/null
