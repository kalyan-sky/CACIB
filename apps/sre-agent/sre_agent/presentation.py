"""The 'at a glance' view of an incident, shared by the email, the dashboard and the RCA email.

Turns a report into plain-English pieces: a headline, where it is, its status, who owns it,
whether it was seen before, what the confidence rests on, and the agent's measured track record.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import IncidentReport

# incident type -> what is wrong, in words an on-call engineer reads in one second
HEADLINES = {
    "OOMKilled": "keeps getting killed: it needs more memory than its limit",
    "CrashLoopBackOff": "keeps crashing at start-up",
    "CreateContainerConfigError": "cannot start: a Secret or ConfigMap reference is broken",
    "ImagePullBackOff": "cannot start: its container image cannot be pulled",
    "PodPending": "cannot be scheduled on any node",
    "PersistentVolumeClaimPending": "is waiting for storage that cannot be provisioned",
    "ReadinessProbeFailure": "is running but receives no traffic (readiness check failing)",
    "LivenessProbeFailure": "is being restarted by its own health check",
    "ResourceQuotaExceeded": "cannot create pods: the namespace quota is full",
    "NodeNotReady": "is down: its pods will be moved",
    "NodePressure": "is running out of resources",
    "CoreDNSDown": "cluster DNS is down: name lookups fail everywhere",
    "ServiceNoEndpoints": "Service has no healthy pods behind it",
    "CPUThrottling": "is slowed down by its CPU limit",
    "CertificateExpired": "certificate has expired: clients reject the connection",
    "CertificateExpiringSoon": "certificate expires soon",
    "TLSHostnameMismatch": "certificate is for the wrong hostname: clients reject it",
    "TLSUntrustedCertificate": "certificate is not trusted by clients",
    "TLSEndpointUnreachable": "TLS endpoint is not answering",
}

BASIS = {
    "confirmed": "stated directly by {source}",
    "probable": "inferred from {source}; verify before acting",
    "inconclusive": "no known failure pattern matched the evidence",
}


def _fmt(value: str | None) -> str:
    try:
        dt = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc) \
            .strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return value or "-"


def subject_of(r: IncidentReport) -> str:
    for prefix in ("deployment/", "node/", "pvc/", "pod/"):
        hit = next((a.split("/", 1)[1] for a in r.affected_resources if a.startswith(prefix)), None)
        if hit:
            return hit if prefix != "node/" else f"Node {hit}"
    return r.namespace or r.cluster


def headline(r: IncidentReport) -> str:
    what = HEADLINES.get(r.incident_type)
    if what:
        return f"{subject_of(r)} {what}"
    return r.incident_summary.split(". ")[0].rstrip(".") if r.incident_summary else "Alert needs investigation"


def confidence_word(c: float) -> str:
    return "High" if c >= 0.85 else ("Medium" if c >= 0.6 else "Low")


def track_record_text(r: IncidentReport) -> str:
    t = r.track_record or {}
    if not t.get("reviewed"):
        return f"{r.incident_type}: not measured yet (no signed-off RCAs of this type)"
    return (f"{r.incident_type}: agent confirmed correct in {t['correct']} of {t['reviewed']} signed-off RCAs"
            + (f" ({t['partial']} partly)" if t.get("partial") else ""))


def view(r: IncidentReport, incident: dict[str, Any] | None = None, approval_status: str | None = None,
         dashboard_url: str | None = None) -> dict[str, Any]:
    incident = incident or {}
    evidence = incident.get("evidence") or {}
    labels = (evidence.get("deployment") or {}).get("labels") or {}
    duration = evidence.get("duration_seconds")
    status = incident.get("status") or "open"
    if r.commands_for_human_execution:
        approval = {"Pending": "fix awaiting approval", "Approved": "fix approved (run by an engineer)",
                    "Rejected": "proposed fix rejected", "Expired": "approval expired"}.get(
            approval_status or "Pending", f"approval {approval_status}")
    else:
        approval = "no fix proposed: the cause is not established" if r.root_cause_status == "inconclusive" \
            else "no change needed from the agent's side"
    first = r.supporting_evidence[0].source if r.supporting_evidence else "the alert"
    seen = r.similar_incidents[0] if r.similar_incidents else None
    base = (dashboard_url or "").rstrip("/")
    return {
        "headline": headline(r),
        "status_line": " · ".join(x for x in [status.capitalize(),
                                               f"investigated in {duration:g} s" if duration else "", approval] if x),
        "where": " › ".join(x for x in [f"{r.cluster} ({incident.get('environment') or '-'})", r.namespace,
                                         (r.affected_resources or [None])[0]] if x),
        "started": _fmt(r.started_at),
        "owner": labels.get("bank.sim/owner-team") or labels.get("team") or "-",
        "seen_line": (f"Yes: {seen['incident_id']} on {seen['date']}"
                      + (f", fixed in {seen['time_to_resolve_min']} min" if seen.get("time_to_resolve_min") is not None else "")
                      + (f" ({len(r.similar_incidents)} similar)" if len(r.similar_incidents) > 1 else "")
                      + (f". Recurring: {r.recurrences_30d} in 30 days" if r.recurrences_30d >= 2 else ""))
        if seen else "No similar incidents on record",
        "confidence_word": confidence_word(r.root_cause_confidence),
        "confidence_basis": BASIS[r.root_cause_status].format(source=first),
        "track_record": track_record_text(r),
        "links": {"incident": f"{base}/incidents/{r.incident_id}", "dashboard": f"{base}/",
                  "rca": f"{base}/incidents/{r.incident_id}/rca"} if base else None,
    }
