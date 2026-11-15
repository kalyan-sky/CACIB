"""Similar past incidents: what happened last time, what fixed it, and was the agent right?

Ranking (no AI needed): same failure type, same workload, same namespace, shared alert
names, overlap of the root-cause wording, and a bonus for RCAs an engineer signed off.
When an engineer said the agent was wrong, the engineer's root cause is shown, so a
correction made once helps every later incident.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import INCONCLUSIVE, IncidentReport
from .store import Store

_WORD = re.compile(r"[a-z0-9][a-z0-9.\-/]{2,}")
_STOP = {"the", "and", "for", "its", "with", "from", "that", "this", "was", "are", "not", "has", "have", "into"}


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def _target(correlation_key: str) -> str:
    """'cluster|workload/ns/name' -> 'workload/ns/name' (the same app in UAT and prod matches)."""
    return correlation_key.split("|", 1)[-1]


def _ts(value: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _minutes(a: str | None, b: str | None) -> int | None:
    x, y = _ts(a), _ts(b)
    return int((y - x).total_seconds() // 60) if x and y and y >= x else None


def find_similar(store: Store, incident: dict[str, Any], report: IncidentReport, limit: int = 3) -> list[dict[str, Any]]:
    me_target, me_alerts = _target(incident["correlation_key"]), set(incident.get("alertnames") or [])
    me_words = _words("" if report.root_cause_status == "inconclusive" else report.root_cause)
    scored = []
    for past in store.history_candidates(report.incident_type, report.namespace, exclude=incident["id"]):
        pr = past.get("report") or {}
        score, why = 0.0, []
        same_type = pr.get("incident_type") == report.incident_type and report.incident_type != "unknown"
        if same_type:
            score += 3
            why.append("same failure type")
        if _target(past["correlation_key"]) == me_target:
            score += 3
            why.append("same workload")
        elif past.get("namespace") and past.get("namespace") == report.namespace:
            score += 1
            why.append("same namespace")
        shared = me_alerts & set(past.get("alertnames") or [])
        if shared:
            score += 1
            why.append(f"alert {sorted(shared)[0]}")
        cause = past.get("root_cause_confirmation") if past.get("agent_verdict") in ("incorrect", "partial") \
            and past.get("root_cause_confirmation") else pr.get("root_cause", "")
        if me_words and cause and INCONCLUSIVE not in cause:
            overlap = len(me_words & _words(cause)) / max(1, len(me_words | _words(cause)))
            score += 2 * overlap
        if past.get("signed_off_by"):
            score += 1
        if not (same_type or "same workload" in why) or score < 4:
            continue  # only show history that is genuinely about the same kind of problem
        scored.append((score, past, cause, why))

    scored.sort(key=lambda x: (x[0], x[1]["seq"]), reverse=True)
    out = []
    for score, past, cause, why in scored[:limit]:
        out.append({
            "incident_id": past["id"],
            "date": (past.get("created_at") or "")[:10],
            "cluster": past["cluster"],
            "workload": _target(past["correlation_key"]).split("/", 1)[-1],
            "incident_type": (past.get("report") or {}).get("incident_type"),
            "root_cause": cause,
            "root_cause_source": "engineer" if past.get("agent_verdict") in ("incorrect", "partial")
            and past.get("root_cause_confirmation") else ("agent, confirmed by engineer"
                                                          if past.get("agent_verdict") == "correct" else "agent"),
            "fix": past.get("actions_taken") or "",
            "time_to_resolve_min": _minutes(past.get("created_at"), past.get("resolved_at")),
            "agent_verdict": past.get("agent_verdict") or "",
            "signed_off": bool(past.get("signed_off_by")),
            "status": past.get("status"),
            "match": ", ".join(why),
            "score": round(score, 2),
        })
    return out


def recurrence(store: Store, incident: dict[str, Any], report: IncidentReport, days: int = 30) -> int:
    """Earlier incidents of the same type on the same workload in the last `days` days."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    me = _target(incident["correlation_key"])
    return sum(1 for p in store.history_candidates(report.incident_type, report.namespace, exclude=incident["id"])
               if _target(p["correlation_key"]) == me and (p.get("report") or {}).get("incident_type")
               == report.incident_type and (_ts(p.get("created_at")) or since) > since)
