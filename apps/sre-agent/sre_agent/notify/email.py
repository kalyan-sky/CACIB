"""HTML incident emails over SMTP (e.g. Gmail with an App Password), de-duplicated."""

from __future__ import annotations

import hashlib
import logging
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import metrics
from ..config import Settings
from ..models import KIND_LABELS, IncidentReport, severity_rank
from ..store import Store

log = logging.getLogger(__name__)

_env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                   autoescape=select_autoescape(["html"]))


def subject(report: IncidentReport) -> str:
    return (f"[{report.severity}] Kubernetes Incident Detected - {report.cluster} - "
            f"{report.namespace or 'cluster-wide'}")


def dedup_key(report: IncidentReport, correlation_key: str) -> str:
    """Same thing, same diagnosis -> one email per window."""
    basis = f"{correlation_key}|{report.incident_type}|{report.root_cause[:200]}"
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def render_html(report: IncidentReport, environment: str, approval_status: str, link: str | None) -> str:
    return _env.get_template("incident_email.html").render(
        r=report, environment=environment, approval_status=approval_status, link=link, labels=KIND_LABELS)


def render_text(report: IncidentReport, approval_status: str, link: str | None) -> str:
    lines = [subject(report), "", f"Incident: {report.incident_id}", f"Severity: {report.severity}",
             f"Cluster: {report.cluster}", f"Namespace: {report.namespace or '-'}",
             f"Affected: {', '.join(report.affected_resources) or '-'}", "",
             f"Summary: {report.incident_summary}",
             f"Root cause ({report.root_cause_status}, {round(report.root_cause_confidence * 100)}%): {report.root_cause}",
             "", "Evidence:"]
    lines += [f"  - [{e.source}] {e.observation}" for e in report.supporting_evidence] or ["  none"]
    lines += ["", "Resolution guide:"]
    for step in report.resolution_steps:
        lines.append(f"  {step.title}")
        for c in step.commands:
            lines.append(f"    [{KIND_LABELS[c.kind]}] {c.command}")
    lines += ["", f"Risk: {report.risk_assessment}", f"Approval: {approval_status} (the agent changed nothing)"]
    if link:
        lines.append(f"Dashboard: {link}")
    return "\n".join(lines)


class EmailNotifier:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    def notify(self, report: IncidentReport, *, environment: str, correlation_key: str,
               approval_status: str, force: bool = False) -> str:
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
        msg["From"] = s.smtp.sender
        msg["To"] = ", ".join(to)
        msg["X-SRE-Incident"] = report.incident_id
        msg.set_content(render_text(report, approval_status, link))
        msg.add_alternative(render_html(report, environment, approval_status, link), subtype="html")
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
        self.store.record_email(key, report.incident_id)
        return "sent"


class EmailError(RuntimeError):
    pass
