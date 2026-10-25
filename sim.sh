#!/usr/bin/env bash
# On-call simulator: work incident and change tickets against the bank cluster.
#
#   ./sim.sh list                 all tickets and your progress
#   ./sim.sh start <id>           inject the problem and print the ticket
#   ./sim.sh page                 start a random unsolved incident (no peeking at the list!)
#   ./sim.sh ticket <id>          print the ticket again
#   ./sim.sh hint <id>            triage hints
#   ./sim.sh check <id>           verify your fix (records time-to-resolve)
#   ./sim.sh solution <id>        reference solution and lessons learned
#   ./sim.sh reset <id>           restore the affected apps to their baseline
#   ./sim.sh status               estate health and customer-facing SLOs
#   ./sim.sh dashboard            port-forward the service-health page to localhost:8080
source "$(dirname "$0")/scripts/lib.sh"

STATE="$SIM_STATE"
mkdir -p "$STATE"

scenario_dir() {
  local id="$1" d
  [ -n "$id" ] || die "scenario id required, e.g. ./sim.sh start 01"
  d=$(find "$ROOT/scenarios" -maxdepth 1 -type d -name "${id}-*" | head -1)
  [ -n "$d" ] || d=$(find "$ROOT/scenarios" -maxdepth 1 -type d -name "${id}" | head -1)
  [ -n "$d" ] || die "no scenario '$id' (see ./sim.sh list)"
  echo "$d"
}

meta() { (source "$1/scenario.env" && eval "echo \"\$$2\""); }

show() {
  local f="$1"
  if command -v glow >/dev/null 2>&1; then glow "$f"; else cat "$f"; fi
}

cmd_list() {
  printf '%s%-4s %-9s %-4s %-66s %s%s\n' "$C_BLD" "ID" "TYPE" "PRI" "TITLE" "STATUS" "$C_RST"
  for d in "$ROOT"/scenarios/*/; do
    d=${d%/}
    local id status
    id=$(basename "$d" | cut -d- -f1)
    if [ -f "$STATE/$id.solved" ]; then
      status="${C_GRN}solved in $(cat "$STATE/$id.solved")${C_RST}"
    elif [ -f "$STATE/$id.started" ]; then
      status="${C_YLW}in progress${C_RST}"
    else
      status="${C_DIM}open${C_RST}"
    fi
    printf '%-4s %-9s %-4s %-66s %s\n' "$id" "$(meta "$d" TYPE)" "$(meta "$d" PRIORITY)" "$(meta "$d" TITLE)" "$status"
  done
}

print_ticket() {
  local d="$1" id
  id=$(basename "$d" | cut -d- -f1)
  echo
  printf '%s%s%s\n' "$C_BLD" "──────────────────────────────────────────────────────────────────────────" "$C_RST"
  printf '%s %s | %s | opened %s%s\n' "$C_BLD" "$(meta "$d" TICKET)" "$(meta "$d" PRIORITY)" \
    "$(cat "$STATE/$id.opened" 2>/dev/null || date '+%d %b %Y %H:%M')" "$C_RST"
  printf '%s%s%s\n' "$C_BLD" "──────────────────────────────────────────────────────────────────────────" "$C_RST"
  # break.sh may record values (e.g. NODE=worker2) that the ticket refers to as {{NODE}}
  local tmp="$STATE/ticket.md"
  cp "$d/ticket.md" "$tmp"
  if [ -f "$STATE/$id.vars" ]; then
    while IFS='=' read -r key val; do
      [ -n "$key" ] && sed -i.bak "s|{{$key}}|$val|g" "$tmp"
    done <"$STATE/$id.vars"
  fi
  show "$tmp"
  rm -f "$tmp" "$tmp.bak"
  echo
  printf '%sWhen you think it is fixed: ./sim.sh check %s%s\n' "$C_DIM" "$id" "$C_RST"
}

cmd_start() {
  local d id
  d=$(scenario_dir "${1:-}")
  id=$(basename "$d" | cut -d- -f1)
  require_cluster
  for f in "$STATE"/*.started; do
    [ -e "$f" ] || continue
    local other
    other=$(basename "$f" .started)
    [ "$other" = "$id" ] || [ -f "$STATE/$other.solved" ] ||
      warn "scenario $other is still in progress - two incidents at once is realistic, but harder to reason about"
  done
  info "Injecting scenario $id ..."
  bash "$d/break.sh" >"$STATE/$id.break.log" 2>&1 || { cat "$STATE/$id.break.log"; die "failed to inject scenario $id"; }
  date +%s >"$STATE/$id.started"
  date '+%d %b %Y %H:%M' >"$STATE/$id.opened"
  rm -f "$STATE/$id.solved"
  print_ticket "$d"
}

cmd_page() {
  local open=()
  for d in "$ROOT"/scenarios/*/; do
    d=${d%/}
    local id
    id=$(basename "$d" | cut -d- -f1)
    [ "$(meta "$d" TYPE)" = incident ] && [ ! -f "$STATE/$id.solved" ] && open+=("$id")
  done
  [ "${#open[@]}" -gt 0 ] || die "no unsolved incidents left - well done. './sim.sh reset <id>' to replay one"
  printf '%s*** PAGERDUTY: you are on call. A new incident has been assigned to Platform Engineering. ***%s\n' "$C_RED" "$C_RST"
  cmd_start "${open[$((RANDOM % ${#open[@]}))]}"
}

cmd_check() {
  local d id
  d=$(scenario_dir "${1:-}")
  id=$(basename "$d" | cut -d- -f1)
  require_cluster
  info "Checking $(meta "$d" TICKET): $(meta "$d" TITLE)"
  if bash "$d/check.sh"; then
    if [ -f "$STATE/$id.started" ] && [ ! -f "$STATE/$id.solved" ]; then
      local secs=$(($(date +%s) - $(cat "$STATE/$id.started")))
      printf '%dm%02ds' $((secs / 60)) $((secs % 60)) >"$STATE/$id.solved"
      echo "Time to resolve: $(cat "$STATE/$id.solved"). Now write the incident note: ./sim.sh solution $id"
    fi
  else
    return 1
  fi
}

cmd_reset() {
  local d id
  d=$(scenario_dir "${1:-}")
  id=$(basename "$d" | cut -d- -f1)
  require_cluster
  info "Resetting scenario $id"
  bash "$d/reset.sh"
  rm -f "$STATE/$id.started" "$STATE/$id.opened" "$STATE/$id.solved"
}

cmd_dashboard() {
  require_cluster
  info "Service health page on http://localhost:${PORT:-8080}  (Ctrl-C to stop)"
  k -n sim-traffic port-forward svc/traffic-simulator "${PORT:-8080}:8080"
}

case "${1:-help}" in
  list | ls) cmd_list ;;
  start) cmd_start "${2:-}" ;;
  page) cmd_page ;;
  ticket) d=$(scenario_dir "${2:-}") && print_ticket "$d" ;;
  hint | hints) d=$(scenario_dir "${2:-}") && show "$d/hints.md" ;;
  check) cmd_check "${2:-}" ;;
  solution) d=$(scenario_dir "${2:-}") && show "$d/solution.md" ;;
  reset) cmd_reset "${2:-}" ;;
  status) require_cluster && "$PY" "$ROOT/tools/platform.py" status ;;
  dashboard) cmd_dashboard ;;
  *) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0" ;;
esac
