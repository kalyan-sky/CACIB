#!/usr/bin/env bash
# Deploy the bank onto an existing cluster: platform RBAC, render the
# inventory for $IMAGE, apply, wait. Used by setup.sh (kind) and eks-up.sh.
#
#   KUBE_CONTEXT  kubectl context of the target cluster (required)
#   IMAGE         image every app runs, already pullable by the nodes
#   PROFILE       lite | full
source "$(dirname "$0")/lib.sh"

PROFILE="${PROFILE:-lite}"

# Remember the target so sim.sh, scenarios and tools/platform.py use it.
mkdir -p "$SIM_STATE"
echo "$PROFILE" >"$SIM_STATE/profile"
echo "$KUBE_CONTEXT" >"$SIM_STATE/context"
echo "$IMAGE" >"$SIM_STATE/image"

info "Applying cluster RBAC"
k apply -f "$ROOT/platform/cluster-rbac.yaml" >/dev/null

info "Rendering inventory (profile=$PROFILE, image=$IMAGE)"
"$PY" "$ROOT/tools/platform.py" render --profile "$PROFILE" --image "$IMAGE"

info "Deploying applications to $KUBE_CONTEXT"
k apply -R -f "$RENDERED" >/dev/null

info "Waiting for all deployments to become available (this can take a few minutes)"
for ns in $(ls "$RENDERED"); do
  k -n "$ns" wait deploy --all --for=condition=Available --timeout=600s >/dev/null ||
    warn "some deployments in $ns are not available yet"
done

echo
k get nodes -L topology.kubernetes.io/zone
echo
info "Bank simulation is up. Next steps:"
cat <<MSG
    ./sim.sh status        estate health + customer-facing SLOs
    ./sim.sh dashboard     live service-health page on http://localhost:8080
    ./sim.sh list          incident / change tickets to work through
MSG
