#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
if [ -f "$SIM_STATE/07.vars" ]; then
  source "$SIM_STATE/07.vars"
  k uncordon "$NODE" >/dev/null || true
  k label node "$NODE" bank.sim/os-patch-level- >/dev/null 2>&1 || true
  rm -f "$SIM_STATE/07.vars"
fi
restore core-banking/core-ledger
