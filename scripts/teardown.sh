#!/usr/bin/env bash
# Delete the kind cluster and local simulation state.
source "$(dirname "$0")/lib.sh"
kind delete cluster --name "$CLUSTER"
rm -rf "$ROOT/.sim-state"
