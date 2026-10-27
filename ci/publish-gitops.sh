#!/usr/bin/env bash
# Render the manifests for one image and commit them to the GitOps branch,
# which Argo CD deploys. This is the "rendered manifests" pattern: the branch
# holds exactly what runs in the cluster, so every deployment and every
# rollback is a Git commit you can review, diff and revert.
#
#   GITOPS_REPO_URL=https://oauth2:<token>@gitlab.com/<group>/<project>.git \
#     ci/publish-gitops.sh <image:tag>
#
# Optional: GITOPS_BRANCH (default gitops), GITOPS_PATH (default deploy/eks),
#           PROFILE (lite|full, default lite), COMMIT_MESSAGE.
set -euo pipefail

IMAGE="${1:?usage: ci/publish-gitops.sh <image:tag>}"
REPO="${GITOPS_REPO_URL:?set GITOPS_REPO_URL}"
BRANCH="${GITOPS_BRANCH:-gitops}"
DEST="${GITOPS_PATH:-deploy/eks}"
PROFILE="${PROFILE:-lite}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> Rendering profile=$PROFILE for $IMAGE"
python3 "$ROOT/tools/platform.py" render --profile "$PROFILE" --image "$IMAGE" --out "$WORK/rendered"
python3 "$ROOT/tools/platform.py" audit --path "$WORK/rendered" >/dev/null ||
  { echo "rendered manifests fail the platform audit - not publishing"; exit 1; }

if git ls-remote --exit-code --heads "$REPO" "$BRANCH" >/dev/null 2>&1; then
  git clone --quiet --depth 1 --branch "$BRANCH" "$REPO" "$WORK/gitops"
else
  echo "==> Branch $BRANCH does not exist yet - creating it"
  git init --quiet "$WORK/gitops"
  git -C "$WORK/gitops" checkout --quiet -b "$BRANCH"
  git -C "$WORK/gitops" remote add origin "$REPO"
fi

rm -rf "${WORK:?}/gitops/$DEST"
mkdir -p "$WORK/gitops/$DEST"
cp -R "$WORK/rendered/." "$WORK/gitops/$DEST/"
cp "$ROOT/platform/cluster-rbac.yaml" "$WORK/gitops/$DEST/00-cluster-rbac.yaml"
cat >"$WORK/gitops/README.md" <<MD
# GitOps branch - generated, do not edit

Written by CI (\`ci/publish-gitops.sh\`) from the main branch. Argo CD deploys
\`$DEST/\`. To change what runs, change the inventory or code on the main branch;
to roll back, revert a commit on this branch.
MD

cd "$WORK/gitops"
git add -A
if git diff --cached --quiet; then
  echo "==> No changes to deploy"
  exit 0
fi
git -c user.name="${GITLAB_USER_NAME:-bank-sim CI}" -c user.email="${GITLAB_USER_EMAIL:-ci@bank-sim.local}" \
  commit --quiet -m "${COMMIT_MESSAGE:-deploy $IMAGE}"
git push --quiet origin "$BRANCH"
echo "==> Published $(git rev-parse --short HEAD) to $BRANCH:$DEST"
git --no-pager show --stat --format='%s' HEAD | tail -5
