"""HTML incident emails over SMTP (e.g. Gmail with an App Password), de-duplicated."""

from __future__ import annotations

import hashlib
import logging
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import metrics, presentation
from ..config import Settings
from ..models import KIND_LABELS, IncidentReport, severity_rank
from ..rca import Rca
from ..store import Store

log = logging.getLogger(__name__)

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                   autoescape=select_autoescape(["html"]))


def subject(report: IncidentReport) -> str:
    """Fixed format from the requirements (mail filters depend on it); the headline is in the body."""
    return (f"[{report.severity}] Kubernetes Incident Detected - {report.cluster} - "
            f"{report.namespace or 'cluster-wide'}")


def dedup_key(report: IncidentReport, correlation_key: str) -> str:
    """Same thing, same diagnosis -> one email per window."""
    basis = f"{correlation_key}|{report.incident_type}|{report.root_cause[:200]}"
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def render_html(report: IncidentReport, environment: str, approval_status: str, link: str | None,
                script: str | None = None, incident: dict | None = None, dashboard_url: str | None = None) -> str:
    v = presentation.view(report, {"environment": environment, **(incident or {})}, approval_status, dashboard_url)
    return _env.get_template("incident_email.html").render(r=report, v=v, labels=KIND_LABELS)


def render_text(report: IncidentReport, approval_status: str, link: str | None, incident: dict | None = None,
                environment: str = "-") -> str:
    """Plain-text alternative, same section order as the HTML."""
    v = presentation.view(report, {"environment": environment, **(incident or {})}, approval_status)
    rule = "-" * 72
    out = [f"[{report.severity}] {report.incident_id} · {report.incident_type}", v["headline"], rule]
    for k in ("status_line", "where", "started", "owner", "seen_line"):
        label = {"status_line": "Status", "where": "Where", "started": "Started", "owner": "Owner",
                 "seen_line": "Seen before"}[k]
        out.append(f"{label:<12} {v[k]}")
    out.append(f"{'Impact':<12} {report.business_impact}")
    out += ["", "1 · WHAT HAPPENED", report.incident_summary, "",
            "2 · ROOT CAUSE", report.root_cause,
            f"Confidence: {v['confidence_word']} ({round(report.root_cause_confidence * 100)}%), "
            f"{report.root_cause_status}, {v['confidence_basis']}",
            f"Track record: {v['track_record']}"]
    if report.possible_contributing_factors:
        out.append("Possible contributing factors (not verified):")
        out += [f"  - {f.replace(' (hypothesis)', '')}" for f in report.possible_contributing_factors]
    out += ["", "3 · EVIDENCE (OBSERVED)"]
    out += [f"  [{e.source}] {e.observation}" for e in report.supporting_evidence] or ["  nothing conclusive"]
    out += ["", "4 · SEEN BEFORE"]
    out += [f"  {s['incident_id']} ({s['date']}): {s['root_cause']} | fixed by: {s.get('fix') or 'not recorded'}"
            for s in report.similar_incidents] or ["  no similar incidents on record"]
    out += ["", "5 · WHAT TO DO"]
    if report.commands_for_human_execution:
        out.append(f"  Fastest: save fix-{report.incident_id}.sh.txt as fix.sh, run: bash fix.sh  "
                   "(checks the cluster, asks before each change, verifies; --check / --rollback)")
    else:
        out.append("  No fix proposed: the cause is not established. Run the checks and record findings in the RCA.")
    n = 0
    for step in report.resolution_steps:
        for c in step.commands:
            n += 1
            tag = {"diagnostic": "CHECK", "remediation": "FIX (needs approval)", "verification": "VERIFY",
                   "rollback": "ROLLBACK (only if worse)"}[c.kind]
            out += [f"  {n}. {tag}: {c.explanation}", f"     {c.command}"]
    out += ["", "6 · RISK AND PREREQUISITES", report.risk_assessment]
    out += [f"  - {p}" for p in report.prerequisites]
    out += ["", rule, "The agent made no changes. Fix and rollback commands are proposals for an authorised engineer."]
    if link:
        out.append(f"Review and approve: {link}")
    return "\n".join(out)


class EmailNotifier:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    def notify(self, report: IncidentReport, *, environment: str, correlation_key: str,
               approval_status: str, force: bool = False, script: str | None = None,
               incident: dict | None = None) -> str:
        """Send the report. Returns the email status stored on the incident."""
        s = self.settings
        if severity_rank(report.severity) > severity_rank(s.email_min_severity):
            return "skipped"
        if not s.smtp.enabled:
            return "skipped"
        to = s.recipients_for(environment, report.severity)
        if not to:
            log.warning("no recipients configured", extra={"incident": report.incident_id})
            return "skipped"
        key = dedup_key(report, correlation_key)
        if not force and self.store.email_recently_sent(key, s.email_dedup_minutes):
            return "deduplicated"

        link = f"{s.dashboard_url.rstrip('/')}/incidents/{report.incident_id}" if s.dashboard_url else None
        msg = EmailMessage()
        msg["Subject"] = subject(report)
        msg["To"] = ", ".join(to)
        msg["X-SRE-Incident"] = report.incident_id
        msg.set_content(render_text(report, approval_status, link, incident, environment))
        msg.add_alternative(render_html(report, environment, approval_status, link, script, incident, s.dashboard_url),
                            subtype="html")
        if script and report.commands_for_human_execution:
            # .txt: mail filters often strip scripts; save it as fix.sh and run it with bash
            msg.add_attachment(script.encode(), maintype="text", subtype="plain",
                               filename=f"fix-{report.incident_id}.sh.txt")
        self._send(msg)
        self.store.record_email(key, report.incident_id)
        return "sent"

    def send_rca(self, rca: Rca, markdown: str, final: bool) -> str:
        """The RCA as an HTML email with the Markdown version attached (for tickets and wikis)."""
        s = self.settings
        if not s.smtp.enabled or severity_rank(rca.severity) > severity_rank(s.email_min_severity):
            return "skipped"
        to = s.recipients_for(rca.environment, rca.severity)
        if not to:
            return "skipped"
        link = f"{s.dashboard_url.rstrip('/')}/incidents/{rca.incident_id}/rca" if s.dashboard_url else None
        msg = EmailMessage()
        msg["Subject"] = (f"[{'FINAL RCA' if final else 'RCA FOR REVIEW'}] {rca.incident_id} - {rca.title} - "
                          f"{rca.namespace or 'cluster-wide'}")
        msg["To"] = ", ".join(to)
        msg["X-SRE-Incident"] = rca.incident_id
        msg.set_content(markdown)
        msg.add_alternative(_env.get_template("rca_email.html").render(r=rca, link=link, final=final), subtype="html")
        msg.add_attachment(markdown.encode(), maintype="text", subtype="markdown",
                           filename=f"RCA-{rca.incident_id}.md")
        self._send(msg)
        return "sent"

    def _send(self, msg: EmailMessage) -> None:
        s = self.settings
        msg["From"] = s.smtp.sender
        try:
            with smtplib.SMTP(s.smtp.host or "", s.smtp.port, timeout=s.smtp.timeout_seconds) as smtp:
                if s.smtp.starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if s.smtp.username and s.smtp.password:
                    smtp.login(s.smtp.username, s.smtp.password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as e:
            metrics.EMAIL_FAILURES.inc()
            # never log the password; SMTP errors don't contain it
            raise EmailError(f"{type(e).__name__}: {str(e)[:300]}") from None
        metrics.EMAILS_SENT.inc()


class EmailError(RuntimeError):
    pass
