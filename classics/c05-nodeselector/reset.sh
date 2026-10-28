#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
k label nodes --all disktype- >/dev/null 2>&1 || true
