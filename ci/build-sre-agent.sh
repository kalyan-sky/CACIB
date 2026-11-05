#!/usr/bin/env bash
# Build the SRE agent image without Docker and push it.
#
#   ci/build-sre-agent.sh <image:tag>
#
# Layers the agent and its pinned dependencies onto python:3.13-slim with
# crane: no Docker daemon, so it runs on a jump host or any CI runner. Wheels
# are fetched for the image's platform (linux/amd64, CPython 3.13), not the
# build machine's. Registry login: GitLab CI variables, or ECR via the AWS CLI.
set -euo pipefail

IMAGE="${1:?usage: ci/build-sre-agent.sh <image:tag>}"
BASE="${SRE_AGENT_BASE_IMAGE:-python:3.13-slim}"
CRANE_VERSION="v0.20.2"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/apps/sre-agent"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if ! command -v crane >/dev/null 2>&1; then
  echo "==> Downloading crane $CRANE_VERSION"
  curl -fsSL "https://github.com/google/go-containerregistry/releases/download/$CRANE_VERSION/go-containerregistry_Linux_x86_64.tar.gz" |
    tar -xz -C "$WORK" crane
  export PATH="$WORK:$PATH"
fi

echo "==> Installing dependencies for linux/amd64, CPython 3.13"
python3 -m pip install --quiet --disable-pip-version-check --no-compile --break-system-packages \
  --target "$WORK/rootfs/app/deps" --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 \
  --implementation cp --python-version 3.13 --only-binary=:all: -r "$APP/requirements.txt" 2>/dev/null ||
  python3 -m pip install --quiet --disable-pip-version-check --no-compile \
    --target "$WORK/rootfs/app/deps" --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 \
    --implementation cp --python-version 3.13 --only-binary=:all: -r "$APP/requirements.txt"
mkdir -p "$WORK/rootfs/app"
cp -R "$APP/sre_agent" "$WORK/rootfs/app/sre_agent"
find "$WORK/rootfs" -name __pycache__ -prune -exec rm -rf {} +
tar --owner=0 --group=0 --numeric-owner --mode='a+rX,go-w' -C "$WORK/rootfs" -cf "$WORK/layer.tar" app

case "$IMAGE" in
  *.dkr.ecr.*.amazonaws.com/*)
    registry="${IMAGE%%/*}"
    region=$(echo "$registry" | cut -d. -f4)
    aws ecr get-login-password --region "$region" | crane auth login "$registry" -u AWS --password-stdin
    ;;
  *)
    if [ -n "${CI_REGISTRY:-}" ]; then
      crane auth login "$CI_REGISTRY" -u "$CI_REGISTRY_USER" -p "$CI_REGISTRY_PASSWORD"
    fi
    ;;
esac

echo "==> Building $IMAGE on $BASE"
crane append --platform linux/amd64 -b "$BASE" -f "$WORK/layer.tar" -t "$IMAGE.tmp" >/dev/null
crane mutate "$IMAGE.tmp" -t "$IMAGE" \
  --entrypoint python,-m,sre_agent.api \
  --user 10001:10001 --workdir /app \
  --env PYTHONPATH=/app/deps:/app --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONUNBUFFERED=1 \
  --label org.opencontainers.image.title=sre-agent >/dev/null
crane delete "$IMAGE.tmp" 2>/dev/null || true
echo "==> Pushed $(crane digest "$IMAGE") $IMAGE"
