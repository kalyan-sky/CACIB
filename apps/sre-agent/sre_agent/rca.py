"""Root cause analysis (post-incident report) in a fixed, readable format.

The agent drafts it from what it observed; engineers complete the parts only
they know (what they did, confirmation of the cause, lessons, follow-ups) and
sign it off. Every statement is labelled as observed evidence, agent
hypothesis, or human input, so a reviewer can tell them apart.

Sections: summary, impact, timeline, detection, root cause and 5 whys,
evidence, resolution and approval, corrective and preventive actions (CAPA),
lessons learned, open questions, appendix.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import presentation
from .models import INCONCLUSIVE, KIND_LABELS, Evidence, IncidentReport


class TimelineEntry(BaseModel):
    time: str
    source: str  # alertmanager | kubernetes | sre-agent | <engineer>
    event: str


class Why(BaseModel):
    question: str
    answer: str
    basis: Literal["evidence", "hypothesis", "human", "unknown"]


class Action(BaseModel):
    action: str
    kind: Literal["corrective", "preventive", "detective"]
    owner: str = "TBD"
    due: str = "TBD"
    status: str = "Open"


class Rca(BaseModel):
    incident_id: str
    title: str
    headline: str = ""
    status: str  # Draft | Resolved - awaiting sign-off | Final
    severity: str
    cluster: str
    environment: str
    namespace: str | None
    service: str | None
    owner_team: str
    alerts: list[str]
    detected_at: str | None
    resolved_at: str | None
    duration: str
    executive_summary: str
    impact: str
    timeline: list[TimelineEntry]
    detection: str
    root_cause: str
    root_cause_status: str
    root_cause_confidence: float
    five_whys: list[Why]
    evidence: list[Evidence]
    contributing_factors: list[str]
    resolution_proposed: list[str]
    approval: str
    actions_taken: str
    root_cause_confirmation: str
    verification: list[str]
    capa: list[Action]
    lessons: list[str]
    open_questions: list[str]
    signed_off: str
    agent_verdict: str = ""  # engineer's review of the agent's root cause: correct | partial | incorrect
    actual_cause_type: str = ""
    related: list[dict[str, Any]] = Field(default_factory=list)  # similar past incidents
    commands_executed: list[str] = Field(default_factory=list)
    analysis_engine: str = "rules"
    generated_at: str = ""


# Corrective / preventive / detective actions by incident type. Owners and
# dates are left for the post-incident review to assign.
CAPA: dict[str, list[tuple[str, str]]] = {
    "OOMKilled": [
        ("corrective", "Set memory request/limit from measured peak usage plus 25-50% headroom"),
        ("preventive", "Load-test memory before release; review caches and buffers for unbounded growth"),
        ("detective", "Keep ContainerMemoryNearLimit (90% for 10 min) routed to on-call"),
    ],
    "CrashLoopBackOff": [
        ("corrective", "Fix the configuration or code named in the crash log; roll back if the cause is a release"),
        ("preventive", "Validate configuration in the CI pipeline (schema/lint) before it reaches the cluster"),
        ("preventive", "Add a startup check that fails with a clear message for missing settings"),
        ("detective", "Alert on restart spikes per workload (PodRestartSpike)"),
    ],
    "CreateContainerConfigError": [
        ("corrective", "Add the missing Secret/ConfigMap key or object"),
        ("preventive", "Manage secrets from one source (e.g. AWS Secrets Manager + External Secrets) with key names defined once"),
        ("detective", "Alert on CreateContainerConfigError within 2 minutes (PodCreateContainerConfigError)"),
    ],
    "ImagePullBackOff": [
        ("corrective", "Point the workload at an image tag that exists"),
        ("preventive", "Deploy only images the pipeline built and pushed (digest pinning, admission check on registry/tag)"),
        ("detective", "Alert on image pull failures (PodImagePullFailing)"),
    ],
    "PodPending": [
        ("corrective", "Right-size requests, or fix the selector/toleration, so the pod can be scheduled"),
        ("preventive", "LimitRange maximums per namespace; request sizing from measured usage (FinOps review)"),
        ("detective", "Alert on pods Pending > 10 minutes (PodPendingTooLong)"),
    ],
    "PersistentVolumeClaimPending": [
        ("corrective", "Recreate the claim with an existing StorageClass"),
        ("preventive", "Validate StorageClass names per cluster in CI; set a default StorageClass"),
        ("detective", "Alert on PVCs Pending > 5 minutes (PVCPending)"),
    ],
    "ReadinessProbeFailure": [
        ("corrective", "Point the readiness probe at the endpoint the app really serves"),
        ("preventive", "Test probes in the deployment pipeline (smoke test of /readyz)"),
        ("detective", "Alert when a Service has no ready endpoints"),
    ],
    "LivenessProbeFailure": [
        ("corrective", "Add a startup probe sized to the real start-up time"),
        ("preventive", "Platform standard: startup probes for slow starters; liveness only checks the process"),
    ],
    "ResourceQuotaExceeded": [
        ("corrective", "Right-size the workload or raise the quota with budget approval"),
        ("preventive", "Capacity check in the onboarding process (quota headroom)"),
        ("detective", "Alert at 90% quota usage (KubeQuotaAlmostFull)"),
    ],
    "NodeNotReady": [
        ("corrective", "Drain and replace the node; confirm workloads rescheduled"),
        ("preventive", "Spread tier-1 workloads across zones with PodDisruptionBudgets; keep spare capacity for one node"),
        ("detective", "Alert on NotReady nodes and EC2 status-check failures"),
    ],
    "NodePressure": [
        ("corrective", "Relieve the pressure (evict/limit the heavy workload, clean disk) and replace the node if needed"),
        ("preventive", "Limits on every container (memory, ephemeral-storage); log rotation"),
        ("detective", "Alert on node memory/disk above 90%"),
    ],
    "CoreDNSDown": [
        ("corrective", "Restore CoreDNS replicas"),
        ("preventive", "Restrict who can scale kube-system workloads; PodDisruptionBudget for CoreDNS"),
        ("detective", "Synthetic DNS probe from a pod; CoreDNSDown alert"),
    ],
    "ServiceNoEndpoints": [
        ("corrective", "Fix the Service selector or targetPort"),
        ("preventive", "Render Services and Deployments from one template (golden path) so labels cannot drift"),
    ],
    "CPUThrottling": [
        ("corrective", "Raise the CPU limit (or follow the platform's no-CPU-limit policy) based on measured peaks"),
        ("preventive", "Review CPU limits against p99 usage each quarter"),
    ],
}
_CERT_CAPA = [
    ("preventive", "Automate issuance and renewal (cert-manager with the bank CA / ACM), renewBefore >= 30 days"),
    ("preventive", "Certificate inventory with an owner and renewal path for every certificate"),
    ("detective", "Expiry alerts at 30 / 14 / 3 days and TLS handshake probes on every endpoint"),
]
CAPA.update({
    "CertificateExpiringSoon": [("corrective", "Renew the certificate before its expiry date")] + _CERT_CAPA,
    "CertificateExpired": [("corrective", "Deploy a renewed certificate and restart its consumers")] + _CERT_CAPA,
    "TLSHostnameMismatch": [
        ("corrective", "Deploy a certificate whose DNS names include every name clients use"),
        ("preventive", "Certificate requests list all external and internal (Service) names; CI check of SANs"),
        ("detective", "Strict TLS probes (hostname verification on) for every endpoint")],
    "TLSUntrustedCertificate": [
        ("corrective", "Serve the full chain (leaf + intermediates)"),
        ("preventive", "Distribute the bank's internal root CA to all clients and base images"),
        ("detective", "Strict TLS probes using the clients' trust store")],
})

GENERIC_CAPA = [
    ("corrective", "Confirm the root cause in the post-incident review and record the fix"),
    ("preventive", "Add an automated check (CI, admission policy or test) that would have caught this"),
    ("detective", "Make sure an alert fires before customers notice; check the time to detect in the timeline"),
]


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fmt(value: str | None) -> str | None:
    """One readable time format everywhere: 2026-11-15 09:41:00 UTC."""
    dt = _ts(value)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if dt else value


def _duration(start: str | None, end: str | None) -> str:
    a, b = _ts(start), _ts(end)
    if not a or not b:
        return "ongoing" if a and not b else "unknown"
    if b < a:
        return "unknown (clock mismatch)"
    minutes = int((b - a).total_seconds() // 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def five_whys(r: IncidentReport) -> list[Why]:
    """A short causal chain. Each step says whether it rests on evidence or is a hypothesis."""
    whys = [Why(question="What happened?", answer=r.incident_summary or "See the alert.", basis="evidence")]
    if r.supporting_evidence:
        whys.append(Why(question="Why did it happen? (immediate cause)", answer=r.supporting_evidence[0].observation,
                        basis="evidence"))
    if r.root_cause_status == "inconclusive":
        whys.append(Why(question="Why? (root cause)", answer=INCONCLUSIVE, basis="unknown"))
    else:
        whys.append(Why(question="Why? (root cause)", answer=r.root_cause,
                        basis="evidence" if r.root_cause_status == "confirmed" else "hypothesis"))
    for factor in r.possible_contributing_factors[:2]:
        whys.append(Why(question="Why was that possible? (contributing factor)",
                        answer=factor.replace(" (hypothesis)", "") + " (to be confirmed in the review)",
                        basis="hypothesis"))
    return whys[:5]


def build_rca(incident: dict[str, Any], approvals: list[dict[str, Any]], audit: list[dict[str, Any]],
              notes: dict[str, Any]) -> Rca:
    report = IncidentReport.model_validate(incident["report"]) if incident.get("report") else None
    evidence = incident.get("evidence") or {}
    alert = incident.get("alert") or {}
    labels = (evidence.get("deployment") or {}).get("labels") or \
        ((evidence.get("pods") or [{}])[0].get("labels") if evidence.get("pods") else {}) or {}
    owner = labels.get("bank.sim/owner-team") or labels.get("team") or "TBD"
    service = (evidence.get("targets") or {}).get("deployment") or alert.get("deployment") or alert.get("pod") \
        or alert.get("node")

    # ------------------------------------------------------------ timeline (UTC)
    tl: list[TimelineEntry] = []
    warnings = [e for e in evidence.get("events") or [] if e.get("type") == "Warning" and e.get("last_seen")]
    for e in warnings[-4:]:
        tl.append(TimelineEntry(time=e["last_seen"], source="kubernetes",
                                event=f"{e['object']}: {e['reason']} (event last seen, x{e.get('count', 1)})"))
    if alert.get("starts_at"):
        tl.append(TimelineEntry(time=alert["starts_at"], source="alertmanager",
                                event=f"Alert {alert.get('alertname')} started firing"))
    tl.append(TimelineEntry(time=incident["created_at"], source="sre-agent", event=f"Incident {incident['id']} opened"))
    labels_for = {
        "incident.investigated": "Read-only investigation completed; root cause analysis drafted",
        "email.sent": "Incident report emailed to on-call",
        "email.failed": "Incident email failed (will retry)",
        "approval.approved": "Proposed resolution approved",
        "approval.rejected": "Proposed resolution rejected",
        "approval.expired": "Approval expired without a decision",
        "incident.resolved": "Alert resolved (monitoring back to normal)",
        "rca.updated": "Engineer updated the RCA",
        "rca.signed_off": "RCA signed off (agent diagnosis reviewed)",
        "rca.emailed": "RCA emailed",
    }
    for a in sorted(audit, key=lambda x: x["id"]):
        if a["action"] in labels_for:
            tl.append(TimelineEntry(time=a["ts"], source=a["actor"], event=labels_for[a["action"]]))
    tl.sort(key=lambda t: _ts(t.time) or datetime.min.replace(tzinfo=timezone.utc))
    for t in tl:
        t.time = fmt(t.time) or t.time

    detected = alert.get("starts_at") or incident["created_at"]
    resolved = incident.get("resolved_at")
    signed = notes.get("signed_off_by")
    status = "Final" if signed else ("Resolved - awaiting review and sign-off" if resolved else "Draft - incident open")

    if report:
        summary = (f"{report.incident_summary} Probable root cause ({report.root_cause_status}, "
                   f"{round(report.root_cause_confidence * 100)}% confidence): {report.root_cause}")
        proposed = [f"{KIND_LABELS[c.kind]}: {c.command}" for c in report.commands_for_human_execution] or \
            ["No fix proposed: the root cause is not established. Investigate first (see open questions)."]
        verification = [c.command for c in report.verification_steps]
        capa_items = CAPA.get(report.incident_type, GENERIC_CAPA)
        open_q = []
        if report.root_cause_status != "confirmed":
            open_q.append("Is the root cause confirmed? Record the confirmation (or the real cause) below.")
        open_q += [f"Verify: {f}" for f in report.possible_contributing_factors[:3]]
        open_q += [f"Evidence gap: {n.removeprefix('evidence gap: ')}" for n in report.analysis_notes
                   if n.startswith("evidence gap")][:3]
    else:
        summary = f"Alert {alert.get('alertname')} fired; the investigation did not complete ({incident.get('error')})."
        proposed, verification, capa_items, open_q = [], [], GENERIC_CAPA, ["Why did the investigation fail?"]

    decided = [a for a in approvals if a["status"] != "Pending"]
    if decided:
        a = decided[0]
        approval = f"{a['status']} by {a['approver'] or 'system'} at {fmt(a['decided_at'])}" + \
            (f": {a['comment']}" if a.get("comment") else "") + \
            (f" (reason: {a['rejection_reason']})" if a.get("rejection_reason") else "")
    elif approvals:
        approval = f"Pending (expires {fmt(approvals[0]['expires_at'])})"
    else:
        approval = "Not required: no remediation proposed"

    if signed:
        open_q = []  # answered in the review; the engineer's confirmation is recorded above
    team = owner if owner != "TBD" else "owning team"
    lessons = [x.strip("-• ").strip() for x in (notes.get("lessons") or "").splitlines() if x.strip()] or [
        "To be completed in the post-incident review: what went well, what went badly, where we were lucky."]
    follow = [x.strip("-• ").strip() for x in (notes.get("follow_ups") or "").splitlines() if x.strip()]

    return Rca(
        incident_id=incident["id"],
        title=f"{report.incident_type if report else 'Incident'}: {service or incident.get('namespace') or 'cluster'} "
              f"({incident['cluster']})",
        headline=presentation.headline(report) if report else "",
        status=status,
        severity=incident["severity"],
        cluster=incident["cluster"],
        environment=incident.get("environment") or "-",
        namespace=incident.get("namespace"),
        service=service,
        owner_team=owner,
        alerts=incident.get("alertnames") or [],
        detected_at=fmt(detected),
        resolved_at=fmt(resolved),
        duration=_duration(detected, resolved),
        executive_summary=summary,
        impact=report.business_impact if report else "Unknown.",
        timeline=tl,
        detection=(f"Detected by Prometheus alert(s) {', '.join(incident.get('alertnames') or [])}; "
                   f"alert seen {incident.get('occurrences', 1)} time(s). The agent investigated automatically "
                   "with read-only access."),
        root_cause=report.root_cause if report else INCONCLUSIVE,
        root_cause_status=report.root_cause_status if report else "inconclusive",
        root_cause_confidence=report.root_cause_confidence if report else 0.0,
        five_whys=five_whys(report) if report else [],
        evidence=report.supporting_evidence if report else [],
        contributing_factors=report.possible_contributing_factors if report else [],
        resolution_proposed=proposed,
        approval=approval,
        actions_taken=notes.get("actions_taken") or "Not yet recorded by an engineer.",
        root_cause_confirmation=notes.get("root_cause_confirmation") or "Not yet confirmed by an engineer.",
        verification=verification,
        capa=[Action(action=a, kind=k, owner=team) for k, a in capa_items]  # type: ignore[arg-type]
        + [Action(action=f, kind="corrective", owner=team) for f in follow],
        lessons=lessons,
        open_questions=open_q,
        signed_off=f"{signed} at {fmt(notes.get('signed_off_at'))}" if signed else "Not signed off",
        agent_verdict=notes.get("agent_verdict") or "",
        actual_cause_type=notes.get("actual_cause_type") or "",
        related=report.similar_incidents if report else [],
        commands_executed=[c.removeprefix("read-only: ") for c in (report.agent_actions_taken if report else [])],
        analysis_engine=report.analysis_engine if report else "-",
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )


BASIS = {"evidence": "observed", "hypothesis": "hypothesis", "human": "engineer", "unknown": "not established"}


def to_markdown(r: Rca) -> str:
    """The RCA as a Markdown document (for tickets, Confluence, email attachments)."""
    out = [
        f"# RCA {r.incident_id}: {r.title}",
        "",
        f"| | |\n|---|---|\n| Status | **{r.status}** |\n| Severity | {r.severity} |\n"
        f"| Cluster / environment | {r.cluster} / {r.environment} |\n| Namespace | {r.namespace or '-'} |\n"
        f"| Service | {r.service or '-'} |\n| Owning team | {r.owner_team} |\n| Alerts | {', '.join(r.alerts)} |\n"
        f"| Detected | {r.detected_at or '-'} |\n| Resolved | {r.resolved_at or 'not yet'} |\n"
        f"| Duration | {r.duration} |\n| Signed off | {r.signed_off} |",
        "",
        "## 1. Summary", r.executive_summary, "",
        "## 2. Impact", r.impact, "",
        "## 3. Timeline",
        "| Time | Source | Event |", "|---|---|---|",
        *[f"| {t.time} | {t.source} | {t.event} |" for t in r.timeline],
        "",
        "## 4. Detection", r.detection, "",
        "## 5. Root cause",
        f"**{r.root_cause}**", "",
        f"Status: {r.root_cause_status}, confidence {round(r.root_cause_confidence * 100)}%. "
        f"Engineer confirmation: {r.root_cause_confirmation}", "",
        *([f"Agent diagnosis reviewed by the engineer: **{r.agent_verdict}**"
           + (f" (real type: {r.actual_cause_type})" if r.actual_cause_type else ""), ""] if r.agent_verdict else []),
        "### 5 whys",
        *[f"{i}. **{w.question}** {w.answer} _({BASIS[w.basis]})_" for i, w in enumerate(r.five_whys, 1)],
        "",
        "### Evidence (observed by the agent)",
        *([f"- `{e.source}` {e.observation}" for e in r.evidence] or ["- none collected"]),
        "",
        "### Contributing factors (hypotheses)",
        *([f"- {f}" for f in r.contributing_factors] or ["- none identified"]),
        "",
        "## 6. Resolution",
        "Proposed by the agent (not executed by it):",
        *[f"- {p}" for p in r.resolution_proposed],
        "",
        f"Approval: {r.approval}", "",
        f"Actions taken (engineer): {r.actions_taken}", "",
        "Verification:", *([f"- `{v}`" for v in r.verification] or ["- see the resolution guide"]),
        "",
        "## 7. Corrective and preventive actions",
        "| # | Action | Type | Owner | Due | Status |", "|---|---|---|---|---|---|",
        *[f"| {i} | {a.action} | {a.kind} | {a.owner} | {a.due} | {a.status} |" for i, a in enumerate(r.capa, 1)],
        "",
        *(["## Related incidents (seen before)",
            *[f"- {s['incident_id']} ({s['date']}, {s['cluster']}/{s['workload']}): {s['root_cause']} "
              f"_({s['root_cause_source']})_" + (f"; fixed by: {s['fix']}" if s.get("fix") else "")
              for s in r.related], ""] if r.related else []),
        "## 8. Lessons learned", *[f"- {x}" for x in r.lessons], "",
        "## 9. Open questions", *([f"- {q}" for q in r.open_questions] or ["- none (closed at sign-off)" if r.status == "Final"
                                                                            else "- none"]), "",
        "## Appendix: read-only commands the agent ran",
        "```", *(r.commands_executed or ["none"]), "```",
        "",
        f"_Generated {r.generated_at} by the read-only SRE agent ({r.analysis_engine}). Observed facts, agent hypotheses "
        "and engineer input are labelled; the agent made no changes._",
    ]
    return "\n".join(out) + "\n"
