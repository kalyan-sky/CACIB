---
name: add-incident-type
description: Add a new incident type to the SRE agent end to end (alert rule, checker, runbook, headline, ready-to-run commands, tests). Use when the agent should recognise a new failure pattern.
---
# Add an incident type

Work through every step; an incident type is only done when all are present.

1. **Alert.** Prometheus rule in `platform/monitoring/` (labels `severity`, `sre_severity`,
   and the resource labels the agent needs: `namespace`, `pod`/`deployment`/`node`).
   Run `make alert-coverage` logic offline via `tools/test_alert_coverage.py` if touched.
2. **Evidence.** If new data is needed, add a read-only operation to
   `sre_agent/k8s/readonly.py` ALLOWED_OPERATIONS *and* the RBAC rule (get/list/watch only)
   and to `sre-agent-verify.sh` can-i checks. Collect it in `diagnostics.py` with the deadline.
3. **Checker.** `check_<name>(c: Ctx) -> Finding | None` in `analysis/rules.py`, added to
   `CHECKERS` in priority order. Confidence: `confirmed` only when the evidence states the
   cause directly; otherwise `probable` with what to verify.
4. **Fix.** Ready-to-run commands with computed values (no `<placeholders>`), a `--check`
   and a rollback in the fix script (`fixscript.py`).
5. **Words.** Runbook entry in `analysis/runbooks.py`; one-line headline in
   `presentation.py` HEADLINES.
6. **Tests.** A scenario in `tests/test_scenarios.py` with recorded evidence: the type, the
   confidence status, that commands contain no `<`, and that `bash -n` passes on the fix script.
7. **Docs.** Add the type to "What it detects" in `docs/sre-agent.md`.
8. Run `make sre-agent-test`, then the `safety-reviewer` subagent.
