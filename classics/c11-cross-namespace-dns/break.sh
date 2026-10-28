#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
data="$NS-data"
k create namespace "$data" --dry-run=client -o yaml | k apply -f -
k -n "$data" create deployment backend --image=nginx:1.27-alpine --port=80
k -n "$data" expose deployment backend --port=80
