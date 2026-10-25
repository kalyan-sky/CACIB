#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
restore risk-compliance
for ns in $(k get ns -l bank.sim/trust-zone -o name); do k label "$ns" bank.sim/trust-zone- >/dev/null; done
