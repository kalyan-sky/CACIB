#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
k taint nodes --all dedicated- >/dev/null 2>&1 || true
k label nodes --all team- >/dev/null 2>&1 || true
rm -f "$SIM_STATE/classics/c06.node" "$SIM_STATE/classics/c06.vars"
