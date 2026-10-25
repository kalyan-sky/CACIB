#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
# the team's manifest, pointed at whichever registry this cluster pulls from
sed "s|image: bank-sim/txn-service:1.4.0|image: $IMAGE|" "$(dirname "$0")/trade-docs-ocr.yaml" | k apply -f -
