"""Alert -> incident -> evidence -> analysis -> report -> approval record -> email.

Order matters: the incident is persisted first, the report before the email,
and every stage catches its own failures, so nothing is silently dropped.
Nothing in this module can change a cluster: it only holds KubeReaders.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from . import metrics
from .analysis.llm import LlmAnalyzer
from .analysis.rules import build_report
from .config import Settings
from .diagnostics import DiagnosticEngine
from .k8s.readonly import KubeReader
from .models import AmAlert, IncidentReport, InvestigationEvidence, NormalizedAlert
from .notify.email import EmailError, EmailNotifier
from .store import Store

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, settings: Settings, store: Store, kube_factory: Callable[[str], KubeReader],
                 notifier: EmailNotifier, analyzer: LlmAnalyzer):
        self.settings = settings
        self.store = store
        self.kube_factory = kube_factory  # cluster name -> a reader with THAT cluster's credentials
        self.notifier = notifier
        self.analyzer = analyzer
        self.queue: asyncio.Queue[tuple[str, NormalizedAlert]] = asyncio.Queue(maxsize=1000)
        self._workers: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------ intake (called by the webhook)

    def accept(self, alerts: list[AmAlert], cluster_name: str) -> dict[str, list[str]]:
        cluster = self.settings.cluster(cluster_name)
        if cluster is None:
            raise UnknownCluster(cluster_name)
        created, attached, resolved = [], [], []
        for a in alerts:
            metrics.ALERTS_RECEIVED.labels(cluster.name, a.status).inc()
            na = NormalizedAlert.from_alert(a, cluster.name, cluster.environment, self.settings.critical_namespaces)
            if a.status == "resolved":
                ids = self.store.resolve(na.correlation_key)
                for i in ids:
                    self.store.audit("alertmanager", "incident.resolved", i, {"alertname": na.alertname})
                resolved += ids
                metrics.ALERTS_PROCESSED.labels(cluster.name, "resolved").inc()
                continue
            incident_id, is_new = self.store.open_or_attach(
                cluster=cluster.name, environment=cluster.environment, namespace=na.namespace,
                correlation_key=na.correlation_key, alertname=na.alertname, severity=na.severity,
                alert=na.model_dump())
            if is_new:
                metrics.INCIDENTS.labels(cluster.name, na.severity).inc()
                self.store.audit("alertmanager", "incident.created", incident_id, {"alertname": na.alertname})
                try:
                    self.queue.put_nowait((incident_id, na))
                except asyncio.QueueFull:
                    self.store.mark_failed(incident_id, "investigation queue full")
                metrics.QUEUE_DEPTH.set(self.queue.qsize())
                created.append(incident_id)
            else:
                metrics.ALERTS_PROCESSED.labels(cluster.name, "correlated").inc()
                attached.append(incident_id)
        return {"created": created, "attached": attached, "resolved": resolved}

    # ------------------------------------------------------------------ workers

    def start(self) -> None:
        n = self.settings.limits.max_concurrent_investigations
        self._workers = [asyncio.create_task(self._worker(i)) for i in range(n)]
        self._workers.append(asyncio.create_task(self._housekeeping()))

    async def stop(self) -> None:
        for t in self._workers:
            t.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    async def _worker(self, n: int) -> None:
        while True:
            incident_id, alert = await self.queue.get()
            metrics.QUEUE_DEPTH.set(self.queue.qsize())
            try:
                await asyncio.to_thread(self.process, incident_id, alert)
            except Exception:
                log.exception("worker failed", extra={"incident": incident_id})
            finally:
                self.queue.task_done()

    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(300)
            try:
                self.store.expire_approvals()
                metrics.PENDING_APPROVALS.set(self.store.pending_approval_count())
                for incident_id in self.store.emails_to_retry():
                    await asyncio.to_thread(self.send_email, incident_id)
            except Exception:
                log.exception("housekeeping failed")

    # ------------------------------------------------------------------ one incident (sync, runs in a thread)

    def investigate(self, alert: NormalizedAlert) -> InvestigationEvidence:
        kube = self.kube_factory(alert.cluster)
        if kube.cluster != alert.cluster:  # belt and braces: never cross clusters
            raise RuntimeError(f"reader for {kube.cluster} offered for an alert from {alert.cluster}")
        return DiagnosticEngine(kube, self.settings.limits).investigate(alert)

    def process(self, incident_id: str, alert: NormalizedAlert) -> IncidentReport | None:
        start = time.monotonic()
        try:
            evidence = self._with_timeout(alert)
        except TimeoutError:
            metrics.INVESTIGATION_TIMEOUTS.labels(alert.cluster).inc()
            evidence = InvestigationEvidence(cluster=alert.cluster, namespace=alert.namespace,
                                             errors=["investigation timed out"], truncated=True)
        except Exception as e:
            log.exception("investigation failed", extra={"incident": incident_id})
            evidence = InvestigationEvidence(cluster=alert.cluster, namespace=alert.namespace,
                                             errors=[f"investigation failed: {type(e).__name__}: {e}"])
        metrics.INVESTIGATION_SECONDS.labels(alert.cluster).observe(time.monotonic() - start)

        try:
            t = time.monotonic()
            report = build_report(alert, evidence, incident_id)
            metrics.ANALYSIS_SECONDS.labels("rules").observe(time.monotonic() - t)
            report = self.analyzer.refine(report, evidence)
            self.store.save_investigation(incident_id, evidence.model_dump(), report.model_dump())
        except Exception as e:
            log.exception("analysis failed", extra={"incident": incident_id})
            self.store.mark_failed(incident_id, f"analysis failed: {type(e).__name__}: {e}")
            metrics.ALERTS_PROCESSED.labels(alert.cluster, "failed").inc()
            return None

        if report.commands_for_human_execution:
            first = report.commands_for_human_execution[0]
            self.store.create_approval(
                incident_id,
                proposed_action=f"{first.command}\n\n{first.explanation}",
                evidence_summary="\n".join(f"[{e.source}] {e.observation}" for e in report.supporting_evidence[:8]),
                risk=report.risk_assessment,
                instructions="\n".join(f"{c.label}: {c.command}" for s in report.resolution_steps for c in s.commands),
                ttl_hours=self.settings.approval_ttl_hours)
            metrics.PENDING_APPROVALS.set(self.store.pending_approval_count())
        self.store.audit("sre-agent", "incident.investigated", incident_id,
                         {"type": report.incident_type, "confidence": report.root_cause_confidence,
                          "engine": report.analysis_engine, "api_calls": len(evidence.commands_executed)})
        self.send_email(incident_id)
        metrics.ALERTS_PROCESSED.labels(alert.cluster, "investigated").inc()
        log.info("incident investigated", extra={"incident": incident_id, "cluster": alert.cluster,
                                                 "namespace": alert.namespace, "type": report.incident_type,
                                                 "severity": report.severity, "status": report.root_cause_status,
                                                 "confidence": report.root_cause_confidence,
                                                 "email": (self.store.get(incident_id) or {}).get("email_status")})
        return report

    def _with_timeout(self, alert: NormalizedAlert) -> InvestigationEvidence:
        """The engine stops collecting at its own deadline; this guards against a hung API call."""
        import concurrent.futures

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(self.investigate, alert)
        try:
            return fut.result(timeout=self.settings.limits.investigation_timeout_seconds + 15)
        except concurrent.futures.TimeoutError:
            raise TimeoutError() from None
        finally:
            ex.shutdown(wait=False)  # don't wait for a hung call; its socket timeout ends it

    def send_email(self, incident_id: str, force: bool = False) -> str:
        inc = self.store.get(incident_id)
        if not inc or not inc.get("report"):
            return "skipped"
        report = IncidentReport.model_validate(inc["report"])
        approvals = self.store.approvals(incident_id=incident_id)
        approval_status = approvals[0]["status"] if approvals else "Not required (no remediation proposed)"
        try:
            status = self.notifier.notify(report, environment=inc.get("environment") or "dev",
                                          correlation_key=inc["correlation_key"], approval_status=approval_status,
                                          force=force)
            self.store.set_email_status(incident_id, status)
        except EmailError as e:
            log.warning("email failed", extra={"incident": incident_id, "error": str(e)})
            self.store.set_email_status(incident_id, "failed", str(e))
            status = "failed"
        return status


class UnknownCluster(LookupError):
    pass
