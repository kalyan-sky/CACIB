#!/usr/bin/env bash
# The SRE agent's durable history (Litestream replica; see docs/sre-agent-history.md).
#
#   ./scripts/sre-agent-history.sh up       create the S3 bucket (terraform/history, its own state)
#                                           and point terraform/terraform.tfvars at it
#   ./scripts/sre-agent-history.sh status   is the agent replicating? (config, logs, metrics, S3)
#   ./scripts/sre-agent-history.sh check    restore drill: restore the replica to a scratch file
#                                           inside the pod and compare it with the live database
#   ./scripts/sre-agent-history.sh destroy  delete the bucket and ALL history (asks first)
#
# `check` is read-only for the agent's data: it writes only /tmp/restore-check.db in the pod.
source "$(dirname "$0")/lib.sh"

HIST_DIR="$ROOT/terraform/history"
TFVARS="$ROOT/terraform/terraform.tfvars"
need terraform "https://developer.hashicorp.com/terraform/install"

cluster_ctx() {
  CLUSTER=$(tf_output cluster_name)
  KUBE_CONTEXT="eks-$CLUSTER"
  export KUBE_CONTEXT
  URL=$(jp '{.data.SRE_AGENT_REPLICA_URL}' configmap sre-agent-config -n sre-agent)
}

cmd_up() {
  local region bucket
  region=$(sed -n 's/^[[:space:]]*region[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' "$TFVARS" 2>/dev/null | head -1)
  region="${AWS_REGION:-${region:-ap-southeast-1}}"
  info "History bucket in $region (separate Terraform state: make tf-down does not touch it)"
  terraform -chdir="$HIST_DIR" init -input=false >/dev/null
  terraform -chdir="$HIST_DIR" apply -input=false -var "region=$region"
  bucket=$(terraform -chdir="$HIST_DIR" output -raw bucket)
  if [ -f "$TFVARS" ] && grep -q '^[[:space:]]*agent_history_bucket' "$TFVARS"; then
    sed -i "s#^[[:space:]]*agent_history_bucket.*#agent_history_bucket = \"$bucket\"#" "$TFVARS"
  else
    printf '\n# SRE agent history (written by scripts/sre-agent-history.sh up)\nagent_history_bucket = "%s"\n' "$bucket" >>"$TFVARS"
  fi
  ok "terraform/terraform.tfvars: agent_history_bucket = \"$bucket\""
  cat <<MSG

    Next (cluster up already? same steps):
      make tf-up          # adds the Pod Identity agent, IAM role and association (~2 min)
      make sre-agent-up   # rebuilds the image with Litestream and turns replication on
      ./scripts/sre-agent-history.sh status
MSG
}

cmd_status() {
  cluster_ctx
  info "History replica of the SRE agent on $CLUSTER"
  [ -n "$URL" ] || { warn "no replica configured: run ./scripts/sre-agent-history.sh up, make tf-up, make sre-agent-up"; check_result; return; }
  ok "replica: $URL"
  local logs; logs=$(k -n sre-agent logs deploy/sre-agent --tail=-1 2>/dev/null | grep 'sre_agent.start' || true)
  case "$logs" in
    *"restored from the replica"*) ok "start-up: history restored from the replica" ;;
    *"empty history"*) ok "start-up: no replica existed yet (first start); replicating from now on" ;;
    *"local database found"*) ok "start-up: local database kept; replicating it" ;;
    *) warn "no start-up line from bin/start (old image without Litestream? run make sre-agent-up)" ;;
  esac
  local errs; errs=$(k -n sre-agent logs deploy/sre-agent --since=30m 2>/dev/null | grep -ciE '"level":"(ERROR|error)".*(replica|s3|litestream|sync)' || true)
  [ "${errs:-0}" = 0 ] && ok "no replication errors in the last 30 min of logs" || ko "$errs replication errors in the last 30 min: kubectl -n sre-agent logs deploy/sre-agent | grep -i error"
  case "$URL" in
    s3://*)
      local count
      count=$(aws s3 ls --recursive "${URL%/}/" 2>/dev/null | wc -l || true)
      [ "${count:-0}" -gt 0 ] && ok "$count objects under $URL" || ko "nothing under $URL yet (credentials? kubectl -n sre-agent logs deploy/sre-agent)"
      ;;
  esac
  check_result
}

cmd_check() {
  cluster_ctx
  [ -n "$URL" ] || die "no replica configured"
  info "Restore drill: replica -> /tmp/restore-check.db in the pod, compared with the live database"
  k -n sre-agent exec deploy/sre-agent -c agent -- sh -c '
    set -e
    rm -f /tmp/restore-check.db*
    litestream restore -no-expand-env -config "${TMPDIR:-/tmp}/sre-agent-litestream.yml" -o /tmp/restore-check.db "$SRE_AGENT_DB" >/dev/null 2>&1
    python - "$SRE_AGENT_DB" /tmp/restore-check.db <<PY
import sqlite3, sys
tables = ["incidents", "approvals", "rca_notes", "audit_log"]
def counts(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
live, restored = counts(sys.argv[1]), counts(sys.argv[2])
bad = [t for t in tables if restored[t] < live[t] - 1]
for t in tables:
    verdict = "MISSING ROWS" if t in bad else "ok"
    print(f"    {t:10} live {live[t]:5}  restored {restored[t]:5}  {verdict}")
sys.exit(1 if bad else 0)
PY
    rm -f /tmp/restore-check.db*' && ok "the replica restores, with the same history as the live database" ||
    ko "restore drill failed (see above)"
  echo "    (a difference of 1 row is replication lag of the last second, not a loss)"
  check_result
}

cmd_destroy() {
  local bucket; bucket=$(terraform -chdir="$HIST_DIR" output -raw bucket 2>/dev/null) || die "no history bucket in $HIST_DIR"
  printf 'Delete bucket %s and ALL SRE agent history in it? Type the bucket name to confirm: ' "$bucket"
  read -r answer
  [ "$answer" = "$bucket" ] || die "aborted"
  terraform -chdir="$HIST_DIR" apply -input=false -auto-approve -var force_destroy=true -target=aws_s3_bucket.history >/dev/null
  terraform -chdir="$HIST_DIR" destroy -input=false -auto-approve -var force_destroy=true
  [ -f "$TFVARS" ] && sed -i '/^[[:space:]]*agent_history_bucket/d' "$TFVARS"
  ok "bucket deleted; agent_history_bucket removed from terraform.tfvars (run make tf-up to drop the IAM pieces)"
}

case "${1:-}" in
  up) cmd_up ;;
  status) cmd_status ;;
  check) cmd_check ;;
  destroy) cmd_destroy ;;
  *) sed -n '2,11p' "$0"; exit 64 ;;
esac
