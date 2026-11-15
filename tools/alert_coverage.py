#!/usr/bin/env python3
"""Alert coverage: is everything we monitor alerted, and does every alert reach the SRE agent?

Reads the live Prometheus and Alertmanager (read-only, through kubectl port-forward):
  1. every scrape job, its targets up/down, and the alert rules that use its metrics
  2. alert rules that fail to evaluate (they can never fire)
  3. Alertmanager routing: default receiver, and alerts silenced by routing to "null"

    python3 tools/alert_coverage.py            # or: make alert-coverage
    python3 tools/alert_coverage.py --strict   # exit 1 if any monitored job has no alert rule

Availability of every job is covered by kube-prometheus-stack's TargetDown alert;
"signal rules" are rules that look at the job's own metrics (errors, saturation...).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import random
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Iterator

import yaml

PROM_SVC = "svc/monitoring-kube-prometheus-prometheus"
AM_SVC = "svc/monitoring-kube-prometheus-alertmanager"
AGENT_RECEIVER = "sre-agent"
# Infrastructure jobs whose health is covered by dedicated built-in rules on other jobs' metrics.
INFRA_OK = {"apiserver", "kubelet", "kube-state-metrics", "node-exporter", "coredns"}


@contextlib.contextmanager
def port_forward(context: str | None, service: str, remote: int) -> Iterator[str]:
    port = random.randint(20000, 40000)
    cmd = ["kubectl", *(["--context", context] if context else []), "-n", "monitoring", "port-forward",
           service, f"{port}:{remote}"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(40):
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
            if proc.poll() is not None:
                raise SystemExit(f"port-forward to {service} failed: {proc.stdout.read() if proc.stdout else ''}")
            time.sleep(0.25)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()


def get(base: str, path: str, **params: str) -> Any:
    url = f"{base}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 (localhost port-forward)
        return json.load(r)


# ---------------------------------------------------------------------- analysis (pure, unit-tested)

def rule_uses(query: str, job: str, metric_names: set[str]) -> bool:
    if re.search(rf'job\s*=~?\s*"{re.escape(job)}"', query):
        return True
    return any(re.search(rf"(?<![\w:]){re.escape(m)}(?![\w:])", query) for m in metric_names if m != "up")


def analyze(targets: list[dict[str, Any]], rules: list[dict[str, Any]], metrics_by_job: dict[str, set[str]],
            am_config: str | None) -> dict[str, Any]:
    jobs: dict[str, dict[str, int]] = {}
    for t in targets:
        job = t.get("labels", {}).get("job") or t.get("scrapePool", "?")
        j = jobs.setdefault(job, {"up": 0, "down": 0})
        j["up" if t.get("health") == "up" else "down"] += 1

    alert_rules = [r for r in rules if r.get("type", "alerting") == "alerting"]
    has_target_down = any(r["name"] == "TargetDown" for r in alert_rules)
    coverage = []
    for job, counts in sorted(jobs.items()):
        using = sorted({r["name"] for r in alert_rules if rule_uses(r.get("query", ""), job,
                                                                     metrics_by_job.get(job, set()))})
        coverage.append({"job": job, **counts, "signal_rules": using,
                         "covered": bool(using) or job in INFRA_OK,
                         "availability": has_target_down})
    broken = [{"name": r["name"], "error": r.get("lastError", "")} for r in alert_rules
              if r.get("health") not in (None, "ok", "unknown")]

    routing: dict[str, Any] = {"ok": False, "default_receiver": None, "silenced": [], "agent_receiver": False}
    if am_config:
        cfg = yaml.safe_load(am_config) or {}
        route = cfg.get("route") or {}
        routing["default_receiver"] = route.get("receiver")
        receivers = {r.get("name"): r for r in cfg.get("receivers") or []}
        routing["agent_receiver"] = bool((receivers.get(AGENT_RECEIVER) or {}).get("webhook_configs"))

        def walk(r: dict[str, Any]) -> None:
            for sub in r.get("routes") or []:
                rec = sub.get("receiver", r.get("receiver"))
                if not (receivers.get(rec) or {}).get("webhook_configs") and rec != AGENT_RECEIVER:
                    routing["silenced"].append({"receiver": rec, "matchers": sub.get("matchers") or sub.get("match")})
                walk(sub)

        walk(route)
        routing["ok"] = routing["default_receiver"] == AGENT_RECEIVER and routing["agent_receiver"]

    by_state: dict[str, int] = {}
    for r in alert_rules:
        by_state[r.get("state", "inactive")] = by_state.get(r.get("state", "inactive"), 0) + 1
    return {"jobs": coverage, "broken_rules": broken, "routing": routing, "alert_rules": len(alert_rules),
            "rule_states": by_state, "target_down_rule": has_target_down}


def render(report: dict[str, Any]) -> str:
    lines = [f"{'JOB':44} {'TARGETS':>9}  SIGNAL ALERT RULES", f"{'-' * 44} {'-' * 9}  {'-' * 40}"]
    for j in report["jobs"]:
        targets = f"{j['up']}/{j['up'] + j['down']}"
        mark = "✔" if j["covered"] else "✘"
        rules = ", ".join(j["signal_rules"][:4]) + (f" +{len(j['signal_rules']) - 4}" if len(j["signal_rules"]) > 4 else "")
        if not rules:
            rules = "(covered by built-in node/cluster rules)" if j["job"] in INFRA_OK else "NONE"
        lines.append(f"{mark} {j['job'][:42]:42} {targets:>9}  {rules}")
    lines.append("")
    lines.append(f"Alert rules: {report['alert_rules']} ({', '.join(f'{k} {v}' for k, v in sorted(report['rule_states'].items()))})."
                 f" TargetDown (availability of every job): {'yes' if report['target_down_rule'] else 'MISSING'}")
    if report["broken_rules"]:
        lines.append("✘ Rules failing to evaluate (they can never fire):")
        lines += [f"    {b['name']}: {b['error'][:120]}" for b in report["broken_rules"]]
    r = report["routing"]
    lines.append(f"{'✔' if r['ok'] else '✘'} Alertmanager default receiver: {r['default_receiver']} "
                 f"(agent receiver configured: {'yes' if r['agent_receiver'] else 'NO'})")
    for s in r["silenced"]:
        lines.append(f"    not sent to the agent (by design?): receiver {s['receiver']} for {s['matchers']}")
    down = [j for j in report["jobs"] if j["down"]]
    if down:
        lines.append("✘ Targets down: " + ", ".join(f"{j['job']} ({j['down']})" for j in down))
    uncovered = [j["job"] for j in report["jobs"] if not j["covered"]]
    lines.append(("✘ Monitored without signal alerts: " + ", ".join(uncovered)) if uncovered
                 else "✔ Every monitored job has alert rules")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", help="kubectl context (default: current)")
    ap.add_argument("--strict", action="store_true", help="exit 1 if a monitored job has no alert rules")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args()

    with port_forward(args.context, PROM_SVC, 9090) as prom:
        targets = get(prom, "/api/v1/targets", state="active")["data"]["activeTargets"]
        rules = [r for g in get(prom, "/api/v1/rules", type="alert")["data"]["groups"] for r in g["rules"]]
        jobs = {t.get("labels", {}).get("job") for t in targets} - {None}
        metrics_by_job = {
            job: set(get(prom, "/api/v1/label/__name__/values", **{"match[]": f'{{job="{job}"}}'})["data"])
            for job in jobs}
    with port_forward(args.context, AM_SVC, 9093) as am:
        am_config = get(am, "/api/v2/status").get("config", {}).get("original")

    report = analyze(targets, rules, metrics_by_job, am_config)
    print(json.dumps(report, indent=2, default=list) if args.json else render(report))
    failed = (not report["routing"]["ok"]) or bool(report["broken_rules"])
    if args.strict and any(not j["covered"] for j in report["jobs"]):
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
