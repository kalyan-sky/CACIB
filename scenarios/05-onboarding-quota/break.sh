#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
k apply -f "$(dirname "$0")/trade-docs-ocr.yaml"
