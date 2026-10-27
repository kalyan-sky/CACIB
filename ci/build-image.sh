#!/usr/bin/env bash
# Build the txn-service image without Docker and push it.
#
#   ci/build-image.sh <image:tag> [version]
#
# Same result as apps/txn-service/Dockerfile: a static Go binary on an empty
# base image, UID 10001, linux/amd64. No Docker daemon is needed, so it runs on
# any CI runner (and in AWS CloudShell). Also tags :latest, which scenario 10
# uses to show why unpinned tags are an audit finding.
set -euo pipefail

IMAGE="${1:?usage: ci/build-image.sh <image:tag> [version]}"
VERSION="${2:-${IMAGE##*:}}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if ! command -v crane >/dev/null 2>&1; then
  GOBIN="$WORK/bin" go install github.com/google/go-containerregistry/cmd/crane@v0.20.2
  export PATH="$WORK/bin:$PATH"
fi

echo "==> Compiling txn-service $VERSION (linux/amd64)"
(cd "$ROOT/apps/txn-service" && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  go build -trimpath -ldflags="-s -w -X main.version=$VERSION" -o "$WORK/rootfs/txn-service" .)
chmod 0755 "$WORK/rootfs/txn-service"
tar --owner=0 --group=0 --numeric-owner -C "$WORK/rootfs" -cf "$WORK/layer.tar" txn-service

if [ -n "${CI_REGISTRY:-}" ]; then
  crane auth login "$CI_REGISTRY" -u "$CI_REGISTRY_USER" -p "$CI_REGISTRY_PASSWORD"
fi

echo "==> Pushing $IMAGE"
crane append --new_layer "$WORK/layer.tar" --new_tag "$IMAGE"
crane mutate "$IMAGE" --set-platform linux/amd64 --entrypoint /txn-service \
  --user 10001:10001 --exposed-ports 8080 --tag "$IMAGE"
crane tag "$IMAGE" latest
echo "==> Pushed $(crane digest "$IMAGE")"
