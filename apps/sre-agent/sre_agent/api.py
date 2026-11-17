"""HTTP API: Alertmanager webhook, incidents and approvals API, dashboard, health and metrics."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from . import __version__, metrics, presentation
from .analysis.llm import LlmAnalyzer, make_provider
from .auth import RateLimiter, User, UserDirectory, csrf_token, token_matches
from .config import SEVERITIES, Settings
from .k8s.readonly import KubeReader, ReadOnlyKube
from .models import KIND_LABELS, AmWebhook, IncidentReport
from .notify.email import EmailNotifier
from .pipeline import Pipeline, UnknownCluster
from .store import APPROVAL_STATES, Store

log = logging.getLogger(__name__)
WEB = Path(__file__).parent / "web"


class Decision(BaseModel):
    comment: str = Field(default="", max_length=2000)
    reason: str = Field(default="", max_length=2000)


def create_app(settings: Settings | None = None, *, kube_factory: Callable[[str], KubeReader] | None = None,
               store: Store | None = None, notifier: EmailNotifier | None = None,
               start_workers: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or Store(settings.db_path)
    if not settings.session_secret:
        settings.session_secret = secrets.token_hex(32)  # CSRF tokens then change on restart: acceptable

    def default_factory(name: str) -> KubeReader:
        cluster = settings.clusters.get(name)
        if cluster is None:
            raise UnknownCluster(name)
        return ReadOnlyKube(cluster, settings.limits)  # fresh reader: its command log is per investigation

    pipeline = Pipeline(settings, store, kube_factory or default_factory,
                        notifier or EmailNotifier(settings, store),
                        LlmAnalyzer(make_provider(settings.llm), settings.llm))
    users = UserDirectory(settings.approvers_file)
    limiter = RateLimiter(settings.rate_limit_per_minute)
    basic = HTTPBasic(auto_error=False)
    templates = Jinja2Templates(directory=str(WEB / "templates"))
    templates.env.globals.update(KIND_LABELS=KIND_LABELS, SEVERITIES=SEVERITIES, version=__version__)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_workers:
            pipeline.start()
        metrics.PENDING_APPROVALS.set(store.pending_approval_count())
        _refresh_accuracy()
        yield
        if start_workers:
            await pipeline.stop()

    app = FastAPI(title="Kubernetes SRE agent", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.pipeline, app.state.store, app.state.settings = pipeline, store, settings
    app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        client = request.client.host if request.client else "?"
        if request.url.path not in ("/healthz", "/readyz", "/metrics") and not limiter.allow(client):
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
        if int(request.headers.get("content-length") or 0) > 2_000_000:
            return JSONResponse({"detail": "request too large"}, status_code=413)
        response = await call_next(request)
        response.headers.update({
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'none'; form-action 'self'; "
                                       "frame-ancestors 'none'",
            "Cache-Control": "no-store",
        })
        return response

    def current_user(creds: HTTPBasicCredentials | None = Depends(basic)) -> User:
        if not users.configured:
            raise HTTPException(503, "no dashboard users configured (SRE_AGENT_APPROVERS_FILE)")
        user = users.authenticate(creds.username, creds.password) if creds else None
        if not user:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required",
                                headers={"WWW-Authenticate": 'Basic realm="sre-agent"'})
        return user

    def approver(user: User = Depends(current_user)) -> User:
        if not user.can_approve:
            store.audit(user.name, "approval.denied", None, {"reason": "role viewer"})
            raise HTTPException(403, "approver role required")
        return user

    # ------------------------------------------------------------------ health / metrics

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, Any]:
        store.stats()  # the database answers
        return {"status": "ready", "queue": pipeline.queue.qsize()}

    @app.get("/metrics")
    def prom() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ------------------------------------------------------------------ Alertmanager webhook

    @app.post("/api/v1/alerts", status_code=202)
    async def alerts(request: Request, body: AmWebhook, cluster: str | None = Query(default=None, max_length=63)) -> dict[str, Any]:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else None
        if not token_matches(token, settings.webhook_token):
            raise HTTPException(401, "invalid webhook token")
        # Cluster identity: the explicit ?cluster= of this receiver, else the alert's external label.
        names = {cluster or a.labels.get("cluster") or settings.default_cluster for a in body.alerts}
        if len(names) != 1 or None in names:
            raise HTTPException(400, "alerts must identify exactly one configured cluster")
        name = names.pop()
        try:
            return pipeline.accept(body.alerts, name)  # type: ignore[arg-type]
        except UnknownCluster:
            raise HTTPException(400, f"cluster '{name}' is not configured") from None

    # ------------------------------------------------------------------ JSON API

    @app.get("/api/v1/incidents")
    def list_incidents(user: User = Depends(current_user), cluster: str | None = None, namespace: str | None = None,
                       severity: str | None = None, incident_type: str | None = None, status_: str | None = Query(None, alias="status"),
                       since: str | None = None, until: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = store.list(cluster=cluster, namespace=namespace, severity=severity, incident_type=incident_type,
                          status=status_, since=since, until=until, limit=limit)
        return [{k: v for k, v in r.items() if k not in ("evidence", "alert")} for r in rows]

    @app.get("/api/v1/incidents/{incident_id}")
    def get_incident(incident_id: str, user: User = Depends(current_user)) -> dict[str, Any]:
        inc = store.get(incident_id)
        if not inc:
            raise HTTPException(404, "incident not found")
        inc["approvals"] = store.approvals(incident_id=incident_id)
        inc["audit"] = store.audit_log(incident_id)
        return inc

    @app.get("/api/v1/approvals")
    def list_approvals(user: User = Depends(current_user), status_: str | None = Query(None, alias="status")) -> list[dict[str, Any]]:
        if status_ and status_ not in APPROVAL_STATES:
            raise HTTPException(400, f"status must be one of {APPROVAL_STATES}")
        return store.approvals(status=status_)

    def _decide(approval_id: int, approved: bool, user: User, comment: str, reason: str) -> dict[str, Any]:
        if not approved and not reason.strip():
            raise HTTPException(400, "a rejection needs a reason")
        try:
            record = store.decide(approval_id, approved=approved, approver=user.name, comment=comment, reason=reason)
        except KeyError:
            raise HTTPException(404, "approval not found") from None
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        metrics.PENDING_APPROVALS.set(store.pending_approval_count())
        # Deliberately nothing else: approving does not run any command. The engineer executes the fix.
        return {**record, "note": "Recorded only. The agent does not execute remediation; run the approved "
                                  "commands yourself through your change process."}

    @app.post("/api/v1/approvals/{approval_id}/approve")
    def api_approve(approval_id: int, body: Decision = Body(default_factory=Decision),
                    user: User = Depends(approver)) -> dict[str, Any]:
        return _decide(approval_id, True, user, body.comment, "")

    @app.post("/api/v1/approvals/{approval_id}/reject")
    def api_reject(approval_id: int, body: Decision, user: User = Depends(approver)) -> dict[str, Any]:
        return _decide(approval_id, False, user, body.comment, body.reason)

    # ------------------------------------------------------------------ dashboard (server-rendered, no JavaScript)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user: User = Depends(current_user), cluster: str | None = None,
                  namespace: str | None = None, severity: str | None = None, incident_type: str | None = None,
                  status_: str | None = Query(None, alias="status"), since: str | None = None,
                  until: str | None = None) -> HTMLResponse:
        rows = store.list(cluster=cluster or None, namespace=namespace or None, severity=severity or None,
                          incident_type=incident_type or None, status=status_ or None,
                          since=since or None, until=until or None, limit=200)
        return templates.TemplateResponse(request, "index.html", {
            "user": user, "stats": store.stats(), "incidents": rows, "accuracy": store.accuracy()["overall"],
            "clusters": sorted(settings.clusters), "pending": store.approvals(status="Pending")[:10],
            "f": {"cluster": cluster or "", "namespace": namespace or "", "severity": severity or "",
                  "incident_type": incident_type or "", "status": status_ or "", "since": since or "",
                  "until": until or ""}})

    @app.get("/incidents/{incident_id}", response_class=HTMLResponse)
    def incident_page(request: Request, incident_id: str, user: User = Depends(current_user)) -> HTMLResponse:
        inc = store.get(incident_id)
        if not inc:
            raise HTTPException(404, "incident not found")
        report = IncidentReport.model_validate(inc["report"]) if inc.get("report") else None
        approvals = store.approvals(incident_id=incident_id)
        v = presentation.view(report, inc, approvals[0]["status"] if approvals else None) if report else None
        tokens = {a["id"]: csrf_token(settings.session_secret, user.name, f"approval:{a['id']}") for a in approvals}
        return templates.TemplateResponse(request, "incident.html", {
            "user": user, "inc": inc, "r": report, "v": v, "approvals": approvals, "tokens": tokens,
            "script": pipeline.fix_script(incident_id),
            "evidence": inc.get("evidence") or {}, "audit": store.audit_log(incident_id)})

    # ------------------------------------------------------------------ agent accuracy (engineer feedback)

    def _refresh_accuracy() -> None:
        for inc_type, t in store.accuracy()["by_type"].items():
            if t["accuracy"] is not None:
                metrics.DIAGNOSIS_ACCURACY.labels(inc_type).set(t["accuracy"])

    @app.get("/api/v1/accuracy")
    def api_accuracy(user: User = Depends(current_user)) -> dict[str, Any]:
        return store.accuracy()

    @app.get("/accuracy", response_class=HTMLResponse)
    def accuracy_page(request: Request, user: User = Depends(current_user)) -> HTMLResponse:
        return templates.TemplateResponse(request, "accuracy.html", {"user": user, "a": store.accuracy()})

    # ------------------------------------------------------------------ RCA (post-incident report)

    def _rca(incident_id: str):
        built = pipeline.rca(incident_id)
        if not built:
            raise HTTPException(404, "incident not found")
        return built

    @app.get("/api/v1/incidents/{incident_id}/rca")
    def api_rca(incident_id: str, user: User = Depends(current_user)) -> dict[str, Any]:
        return _rca(incident_id)[0].model_dump()

    @app.get("/incidents/{incident_id}/rca.md")
    def rca_markdown(incident_id: str, user: User = Depends(current_user)) -> Response:
        return Response(_rca(incident_id)[1], media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="RCA-{incident_id}.md"'})

    @app.get("/incidents/{incident_id}/fix.sh")
    def fix_script(incident_id: str, user: User = Depends(current_user)) -> Response:
        script = pipeline.fix_script(incident_id)
        if script is None:
            raise HTTPException(404, "no report for this incident yet")
        store.audit(user.name, "fixscript.downloaded", incident_id)
        return Response(script, media_type="text/x-shellscript; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="fix-{incident_id}.sh"'})

    @app.get("/incidents/{incident_id}/rca", response_class=HTMLResponse)
    def rca_page(request: Request, incident_id: str, user: User = Depends(current_user)) -> HTMLResponse:
        doc, _ = _rca(incident_id)
        notes = store.rca_notes(incident_id)
        return templates.TemplateResponse(request, "rca.html", {
            "user": user, "r": doc, "notes": notes, "signed": bool(notes.get("signed_off_by")),
            "csrf": csrf_token(settings.session_secret, user.name, f"rca:{incident_id}")})

    @app.post("/incidents/{incident_id}/rca")
    def rca_update(incident_id: str, csrf: str = Form(...), action: str = Form("save", pattern="^(save|sign_off)$"),
                   actions_taken: str = Form("", max_length=5000), root_cause_confirmation: str = Form("", max_length=5000),
                   lessons: str = Form("", max_length=5000), follow_ups: str = Form("", max_length=5000),
                   agent_verdict: str = Form("", pattern="^(|correct|partial|incorrect)$"),
                   actual_cause_type: str = Form("", max_length=100),
                   user: User = Depends(approver)) -> RedirectResponse:
        if not token_matches(csrf, csrf_token(settings.session_secret, user.name, f"rca:{incident_id}")):
            raise HTTPException(403, "invalid form token")
        if not store.get(incident_id):
            raise HTTPException(404, "incident not found")
        try:
            store.save_rca_notes(incident_id, user.name, {
                "actions_taken": actions_taken, "root_cause_confirmation": root_cause_confirmation,
                "lessons": lessons, "follow_ups": follow_ups, "agent_verdict": agent_verdict,
                "actual_cause_type": actual_cause_type}, sign_off=action == "sign_off")
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        if action == "sign_off":
            inc_type = (store.get(incident_id) or {}).get("incident_type") or "unknown"
            metrics.DIAGNOSIS_FEEDBACK.labels(inc_type, agent_verdict).inc()
            _refresh_accuracy()
            pipeline.send_rca(incident_id, final=True)  # records only; no cluster action
        return RedirectResponse(f"/incidents/{incident_id}/rca", status_code=303)

    @app.post("/incidents/{incident_id}/approvals/{approval_id}")
    def incident_decide(incident_id: str, approval_id: int, decision: str = Form(..., pattern="^(approve|reject)$"),
                        csrf: str = Form(...), comment: str = Form("", max_length=2000),
                        reason: str = Form("", max_length=2000), user: User = Depends(approver)) -> RedirectResponse:
        if not token_matches(csrf, csrf_token(settings.session_secret, user.name, f"approval:{approval_id}")):
            store.audit(user.name, "approval.csrf_rejected", incident_id, {"approval_id": approval_id})
            raise HTTPException(403, "invalid form token")
        if not any(a["id"] == approval_id for a in store.approvals(incident_id=incident_id)):
            raise HTTPException(404, "approval not found for this incident")
        _decide(approval_id, decision == "approve", user, comment, reason)
        return RedirectResponse(f"/incidents/{incident_id}", status_code=303)

    return app


def main() -> None:
    """Serve HTTP on 8080 (Alertmanager, Prometheus, port-forward) and, when a certificate
    is mounted, HTTPS on 8443 for the dashboard behind a load balancer."""
    import asyncio
    import os

    import uvicorn

    from .logging_setup import setup

    setup()
    app = create_app()
    common = dict(log_config=None, access_log=False, proxy_headers=False, server_header=False)
    servers = [uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8080, **common))]
    cert, key = os.environ.get("SRE_AGENT_TLS_CERT"), os.environ.get("SRE_AGENT_TLS_KEY")
    if cert and key and Path(cert).exists() and Path(key).exists():
        servers.append(uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8443, ssl_certfile=cert,
                                                     ssl_keyfile=key, lifespan="off", **common)))

    async def serve() -> None:
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(serve())


if __name__ == "__main__":
    main()
