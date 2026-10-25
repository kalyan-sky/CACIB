#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
k delete -f "$(dirname "$0")/trade-docs-ocr.yaml" --ignore-not-found
restore trade-finance
