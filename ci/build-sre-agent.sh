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
# Litestream: streams the agent's SQLite history to S3 (or any replica, see apps/sre-agent/bin/start).
# Pin the tarball's sha256 in LITESTREAM_SHA256 once you have verified it; until then the
# release's checksums.txt is used and the hash is printed so it can be pinned.
LITESTREAM_VERSION="${LITESTREAM_VERSION:-0.5.18}"
LITESTREAM_SHA256="${LITESTREAM_SHA256:-}"
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
mkdir -p "$WORK/rootfs/app" "$WORK/rootfs/usr/local/bin"
cp -R "$APP/sre_agent" "$WORK/rootfs/app/sre_agent"
cp -R "$APP/bin" "$WORK/rootfs/app/bin"
find "$WORK/rootfs" -name __pycache__ -prune -exec rm -rf {} +

echo "==> Litestream $LITESTREAM_VERSION"
ls_tar="litestream-$LITESTREAM_VERSION-linux-x86_64.tar.gz"
ls_url="https://github.com/benbjohnson/litestream/releases/download/v$LITESTREAM_VERSION"
curl -fsSLo "$WORK/$ls_tar" "$ls_url/$ls_tar"
got=$(sha256sum "$WORK/$ls_tar" | cut -d' ' -f1)
if [ -n "$LITESTREAM_SHA256" ]; then
  [ "$got" = "$LITESTREAM_SHA256" ] || { echo "Litestream checksum mismatch: got $got, pinned $LITESTREAM_SHA256" >&2; exit 1; }
else
  want=$(curl -fsSL "$ls_url/checksums.txt" | awk -v f="$ls_tar" '$2 == f || $2 == "*"f {print $1}')
  [ -n "$want" ] && [ "$got" = "$want" ] || { echo "Litestream checksum mismatch: got $got, release lists ${want:-nothing}" >&2; exit 1; }
  echo "    sha256 $got (matches the release's checksums.txt; pin it with LITESTREAM_SHA256=$got)"
fi
tar -xzf "$WORK/$ls_tar" -C "$WORK/rootfs/usr/local/bin" litestream
chmod 0755 "$WORK/rootfs/usr/local/bin/litestream" "$WORK/rootfs/app/bin/start"

tar --owner=0 --group=0 --numeric-owner --mode='a+rX,go-w' -C "$WORK/rootfs" -cf "$WORK/layer.tar" app usr

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
  --entrypoint /app/bin/start \
  --user 10001:10001 --workdir /app \
  --env PYTHONPATH=/app/deps:/app --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONUNBUFFERED=1 \
  --label org.opencontainers.image.title=sre-agent >/dev/null
crane delete "$IMAGE.tmp" 2>/dev/null || true
echo "==> Pushed $(crane digest "$IMAGE") $IMAGE"
