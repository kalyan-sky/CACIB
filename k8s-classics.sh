#!/usr/bin/env bash
# Classic Kubernetes debugging cases: small, generic, run on any cluster
# (kind, minikube, Killercoda, EKS). Each case lives in its own namespace and
# uses small public images (nginx, busybox, python, curl).
#
#   ./k8s-classics.sh list              all cases and your progress
#   ./k8s-classics.sh start <id>        break it and print the ticket (e.g. c01)
#   ./k8s-classics.sh page              a random unsolved case
#   ./k8s-classics.sh ticket <id>       print the ticket again
#   ./k8s-classics.sh hint <id>         hints, from gentle to explicit
#   ./k8s-classics.sh check <id>        verify your fix (records time to resolve)
#   ./k8s-classics.sh solution <id>     reference solution and what to learn
#   ./k8s-classics.sh reset <id>        delete the case (namespace + any node changes)
#   ./k8s-classics.sh reset-all
#
# Uses your current kubectl context (set KUBE_CONTEXT to override).
KUBE_CONTEXT="${KUBE_CONTEXT:-$(kubectl config current-context 2>/dev/null || true)}"
export KUBE_CONTEXT
source "$(dirname "$0")/scripts/lib.sh"

CASES="$ROOT/classics"
STATE="$SIM_STATE/classics"
mkdir -p "$STATE"

case_dir() {
  local d
  [ -n "${1:-}" ] || die "case id required, e.g. ./k8s-classics.sh start c01"
  d=$(find "$CASES" -maxdepth 1 -type d -name "${1}-*" | head -1)
  [ -n "$d" ] || die "no case '$1' (see ./k8s-classics.sh list)"
  echo "$d"
}
meta() { (source "$1/case.env" && eval "echo \"\${$2:-}\""); }
ns_of() { basename "$1"; } # each case runs in a namespace named after its folder
show() { if command -v glow >/dev/null 2>&1; then glow "$1"; else cat "$1"; fi; }

# render <file> <case-dir>: fill {{NS}} and any values the case recorded (e.g. {{NODE}})
render() {
  local id ns tmp
  id=$(basename "$2" | cut -d- -f1); ns=$(ns_of "$2")
  tmp="$STATE/render.md"
  sed "s/{{NS}}/$ns/g" "$1" >"$tmp"
  if [ -f "$STATE/$id.vars" ]; then
    while IFS='=' read -r key val; do
      [ -n "$key" ] && sed -i.bak "s|{{$key}}|$val|g" "$tmp"
    done <"$STATE/$id.vars"
  fi
  show "$tmp"
  rm -f "$tmp" "$tmp.bak"
}

# EC2 worker nodes (Fargate's per-pod virtual nodes don't count)
has_ec2_nodes() { k get nodes -l 'eks.amazonaws.com/compute-type!=fargate' -o name 2>/dev/null | grep -q .; }
needs_nodes() { [ "$(meta "$1" NEEDS_NODES)" = 1 ]; }

need_cluster() {
  [ -n "$KUBE_CONTEXT" ] || die "no kubectl context - create a cluster first (kind create cluster, or a Killercoda playground)"
  k get --raw /readyz >/dev/null 2>&1 || die "cluster '$KUBE_CONTEXT' is not reachable"
}

cmd_list() {
  printf '%s%-4s %-7s %-34s %-44s %s%s\n' "$C_BLD" ID LEVEL SYMPTOM "" STATUS "$C_RST"
  for d in "$CASES"/c*/; do
    d=${d%/}
    local id st
    id=$(basename "$d" | cut -d- -f1)
    if [ -f "$STATE/$id.solved" ]; then st="${C_GRN}solved in $(cat "$STATE/$id.solved")${C_RST}"
    elif [ -f "$STATE/$id.started" ]; then st="${C_YLW}in progress${C_RST}"
    else st="${C_DIM}open${C_RST}"; fi
    printf '%-4s %-7s %-34s %-44s %s\n' "$id" "$(meta "$d" LEVEL)" "$(meta "$d" SYMPTOM)" "$(meta "$d" TITLE)" "$st"
  done
}

cmd_start() {
  local d id ns
  d=$(case_dir "${1:-}"); id=$(basename "$d" | cut -d- -f1); ns=$(ns_of "$d")
  need_cluster
  if needs_nodes "$d" && ! has_ec2_nodes; then
    die "$id needs EC2 worker nodes, and this cluster has none (Fargate). Skip it, or use compute = \"ec2\"."
  fi
  info "Setting up $id in namespace $ns ..."
  k delete namespace "$ns" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  [ -x "$d/reset.sh" ] && NS="$ns" bash "$d/reset.sh" >/dev/null 2>&1 || true
  k create namespace "$ns" >/dev/null
  if [ -f "$d/broken.yaml" ]; then
    k -n "$ns" apply -f "$d/broken.yaml" >"$STATE/$id.log" 2>&1 || { cat "$STATE/$id.log"; die "could not apply $id"; }
  fi
  if [ -f "$d/break.sh" ]; then
    NS="$ns" bash "$d/break.sh" >>"$STATE/$id.log" 2>&1 || { cat "$STATE/$id.log"; die "could not set up $id"; }
  fi
  date +%s >"$STATE/$id.started"
  rm -f "$STATE/$id.solved"
  cmd_ticket "$id"
}

cmd_ticket() {
  local d id ns
  d=$(case_dir "${1:-}"); id=$(basename "$d" | cut -d- -f1); ns=$(ns_of "$d")
  echo
  printf '%s── %s · %s · namespace %s ──%s\n' "$C_BLD" "$id" "$(meta "$d" TITLE)" "$ns" "$C_RST"
  render "$d/ticket.md" "$d"
  echo
  printf '%sWork in:  kubectl -n %s ...     Verify:  ./k8s-classics.sh check %s%s\n' "$C_DIM" "$ns" "$id" "$C_RST"
}

cmd_check() {
  local d id ns
  d=$(case_dir "${1:-}"); id=$(basename "$d" | cut -d- -f1); ns=$(ns_of "$d")
  need_cluster
  info "Checking $id: $(meta "$d" TITLE)"
  if NS="$ns" bash "$d/check.sh"; then
    if [ -f "$STATE/$id.started" ] && [ ! -f "$STATE/$id.solved" ]; then
      local secs=$(($(date +%s) - $(cat "$STATE/$id.started")))
      printf '%dm%02ds' $((secs / 60)) $((secs % 60)) >"$STATE/$id.solved"
      echo "Time to resolve: $(cat "$STATE/$id.solved"). Read the takeaway: ./k8s-classics.sh solution $id"
    fi
  else
    return 1
  fi
}

cmd_reset() {
  local d id ns
  d=$(case_dir "${1:-}"); id=$(basename "$d" | cut -d- -f1); ns=$(ns_of "$d")
  info "Resetting $id"
  k delete namespace "$ns" --ignore-not-found --wait=false >/dev/null
  [ -f "$d/reset.sh" ] && NS="$ns" bash "$d/reset.sh"
  rm -f "$STATE/$id.started" "$STATE/$id.solved"
}

cmd_page() {
  local open=() nodes=1
  need_cluster
  has_ec2_nodes || nodes=0
  for d in "$CASES"/c*/; do
    local id; id=$(basename "${d%/}" | cut -d- -f1)
    [ "$nodes" = 0 ] && needs_nodes "${d%/}" && continue
    [ -f "$STATE/$id.solved" ] || open+=("$id")
  done
  [ "${#open[@]}" -gt 0 ] || die "all cases solved - reset one to replay it"
  cmd_start "${open[$((RANDOM % ${#open[@]}))]}"
}

case "${1:-help}" in
  list | ls) cmd_list ;;
  start) cmd_start "${2:-}" ;;
  page) cmd_page ;;
  ticket) cmd_ticket "${2:-}" ;;
  hint | hints) d=$(case_dir "${2:-}") && render "$d/hints.md" "$d" ;;
  check) cmd_check "${2:-}" ;;
  solution) show "$(case_dir "${2:-}")/solution.md" ;;
  reset) cmd_reset "${2:-}" ;;
  reset-all) for d in "$CASES"/c*/; do cmd_reset "$(basename "${d%/}" | cut -d- -f1)"; done ;;
  *) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0" ;;
esac
