#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
k delete namespace "$NS-data" --ignore-not-found --wait=false >/dev/null
