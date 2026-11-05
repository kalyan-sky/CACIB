"""Webhook, authentication, approval workflow, dashboard and agent metrics."""

from __future__ import annotations

import re

import pytest
from fakes import NS, FakeKube, am_alert, deployment, pod, webhook
from fastapi.testclient import TestClient

from sre_agent.api import create_app

TOKEN = {"Authorization": "Bearer webhook-token-0123456789"}
ALICE = ("alice", "alice-password-123")
VICTOR = ("victor", "victor-password-123")
POD = "app-7d9f8c6b5d-x7k2p"


@pytest.fixture
def fake():
    return FakeKube(pods=[pod(POD, "app", state={"waiting": {"reason": "CrashLoopBackOff"}},
                              last_state={"terminated": {"reason": "OOMKilled", "exitCode": 137}}, restarts=4)],
                    deployments=[deployment("app")])


@pytest.fixture
def app(settings, store, fake, smtp):
    return create_app(settings, kube_factory=lambda name: fake, store=store, start_workers=False)


def ingest(client, app, *alerts, cluster="test-eks"):
    r = client.post(f"/api/v1/alerts?cluster={cluster}", json=webhook(*alerts), headers=TOKEN)
    assert r.status_code == 202, r.text
    while not app.state.pipeline.queue.empty():
        incident_id, na = app.state.pipeline.queue.get_nowait()
        app.state.pipeline.process(incident_id, na)
    return r.json()


def test_webhook_requires_token(app):
    with TestClient(app) as c:
        assert c.post("/api/v1/alerts?cluster=test-eks", json=webhook()).status_code == 401
        assert c.post("/api/v1/alerts?cluster=test-eks", json=webhook(),
                      headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_webhook_rejects_unknown_or_ambiguous_cluster(app):
    with TestClient(app) as c:
        r = c.post("/api/v1/alerts?cluster=other", json=webhook(am_alert("X", namespace=NS)), headers=TOKEN)
        assert r.status_code == 400
        # no ?cluster= and no default: which credentials? refuse rather than guess
        r = c.post("/api/v1/alerts", json=webhook(am_alert("X", namespace=NS)), headers=TOKEN)
        assert r.status_code == 400
        r = c.post("/api/v1/alerts", json=webhook(am_alert("X", namespace=NS, cluster="test-eks"),
                                                  am_alert("Y", namespace=NS, cluster="uat-eks")), headers=TOKEN)
        assert r.status_code == 400


def test_webhook_validates_payload(app):
    with TestClient(app) as c:
        assert c.post("/api/v1/alerts?cluster=test-eks", json={"alerts": [{"status": "weird"}]},
                      headers=TOKEN).status_code == 422


def test_clusters_are_kept_apart(settings, store, smtp):
    seen = []

    def factory(name):
        seen.append(name)
        return FakeKube(cluster=name)

    app = create_app(settings, kube_factory=factory, store=store, start_workers=False)
    with TestClient(app) as c:
        ingest(c, app, am_alert("KubePodCrashLooping", namespace=NS, pod=POD), cluster="uat-eks")
        ingest(c, app, am_alert("KubePodCrashLooping", namespace=NS, pod=POD), cluster="test-eks")
    assert seen == ["uat-eks", "test-eks"]
    assert {i["cluster"] for i in store.list()} == {"uat-eks", "test-eks"}
    assert len(store.list()) == 2  # same pod name, different clusters: separate incidents


def test_wrong_cluster_reader_is_refused(settings, store, smtp):
    app = create_app(settings, kube_factory=lambda name: FakeKube(cluster="test-eks"), store=store, start_workers=False)
    with TestClient(app) as c:
        ingest(c, app, am_alert("KubePodCrashLooping", namespace=NS, pod=POD), cluster="uat-eks")
    inc = store.list()[0]
    assert "reader for test-eks" in " ".join(inc["evidence"]["errors"])


def test_correlation_dedup_and_resolution(app, store, smtp):
    with TestClient(app) as c:
        first = ingest(c, app, am_alert("KubePodCrashLooping", namespace=NS, pod=POD))
        second = ingest(c, app, am_alert("KubeDeploymentReplicasMismatch", namespace=NS, deployment="app",
                                         fingerprint="fp2"))
        assert second["attached"] == first["created"]  # same workload -> same incident
        inc = store.get(first["created"][0])
        assert inc["occurrences"] == 2 and set(inc["alertnames"]) == {"KubePodCrashLooping", "KubeDeploymentReplicasMismatch"}
        assert len(smtp.sent) == 1  # one email for one incident
        r = c.post("/api/v1/alerts?cluster=test-eks",
                   json=webhook(am_alert("KubePodCrashLooping", status="resolved", namespace=NS, pod=POD)), headers=TOKEN)
        assert r.json()["resolved"] == first["created"]
        assert store.get(first["created"][0])["status"] == "resolved"


def test_email_dedup_across_incidents(app, store, smtp):
    with TestClient(app) as c:
        a = ingest(c, app, am_alert("KubePodCrashLooping", namespace=NS, pod=POD))
        c.post("/api/v1/alerts?cluster=test-eks", headers=TOKEN,
               json=webhook(am_alert("KubePodCrashLooping", status="resolved", namespace=NS, pod=POD)))
        b = ingest(c, app, am_alert("KubePodCrashLooping", namespace=NS, pod=POD))
    assert a["created"] != b["created"]
    assert len(smtp.sent) == 1  # same diagnosis again within the window: no second email
    assert store.get(b["created"][0])["email_status"] == "deduplicated"


def test_email_failure_keeps_the_incident(app, store, smtp):
    smtp.fail = True
    with TestClient(app) as c:
        out = ingest(c, app, am_alert("KubePodCrashLooping", namespace=NS, pod=POD))
    inc = store.get(out["created"][0])
    assert inc["status"] == "open" and inc["report"] and inc["email_status"] == "failed"
    assert store.emails_to_retry() == [inc["id"]]
    smtp.fail = False
    assert app.state.pipeline.send_email(inc["id"]) == "sent"


def test_dashboard_and_api_require_login(app):
    with TestClient(app) as c:
        for path in ("/", "/api/v1/incidents", "/api/v1/approvals", "/incidents/INC-0001"):
            assert c.get(path).status_code == 401
            assert c.get(path, auth=("alice", "wrong")).status_code == 401


def test_viewer_cannot_approve(app, store):
    with TestClient(app) as c:
        out = ingest(c, app, am_alert("PodOOMKilled", namespace=NS, pod=POD))
        approval = store.approvals(incident_id=out["created"][0])[0]
        r = c.post(f"/api/v1/approvals/{approval['id']}/approve", json={}, auth=VICTOR)
        assert r.status_code == 403
        assert store.approvals(incident_id=out["created"][0])[0]["status"] == "Pending"
        assert any(a["action"] == "approval.denied" for a in store.audit_log())


def test_reject_needs_reason_and_is_audited(app, store):
    with TestClient(app) as c:
        out = ingest(c, app, am_alert("PodOOMKilled", namespace=NS, pod=POD))
        aid = store.approvals(incident_id=out["created"][0])[0]["id"]
        assert c.post(f"/api/v1/approvals/{aid}/reject", json={"reason": ""}, auth=ALICE).status_code == 400
        r = c.post(f"/api/v1/approvals/{aid}/reject", json={"reason": "fix the leak instead"}, auth=ALICE)
        assert r.status_code == 200 and r.json()["status"] == "Rejected" and r.json()["approver"] == "alice"
        assert c.post(f"/api/v1/approvals/{aid}/approve", json={}, auth=ALICE).status_code == 409  # decided once
        actions = [a["action"] for a in store.audit_log(out["created"][0])]
        assert "approval.rejected" in actions and "incident.investigated" in actions


def test_approvals_expire(app, store):
    with TestClient(app) as c:
        out = ingest(c, app, am_alert("PodOOMKilled", namespace=NS, pod=POD))
    aid = store.approvals(incident_id=out["created"][0])[0]["id"]
    store._x("UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (aid,))
    assert store.approvals(incident_id=out["created"][0])[0]["status"] == "Expired"
    with pytest.raises(ValueError):
        store.decide(aid, approved=True, approver="alice")


def test_dashboard_pages_and_form_approval_with_csrf(app, store):
    with TestClient(app) as c:
        out = ingest(c, app, am_alert("PodOOMKilled", namespace=NS, pod=POD))
        iid = out["created"][0]
        home = c.get("/?severity=CRITICAL", auth=ALICE)
        assert home.status_code == 200 and iid in home.text and "Pending approvals" in home.text
        assert "default-src 'self'" in home.headers["content-security-policy"]
        page = c.get(f"/incidents/{iid}", auth=ALICE)
        assert page.status_code == 200 and "OOMKilled" in page.text and "NOT executed" in page.text
        aid = store.approvals(incident_id=iid)[0]["id"]
        token = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
        bad = c.post(f"/incidents/{iid}/approvals/{aid}", data={"decision": "approve", "csrf": "forged"},
                     auth=ALICE, follow_redirects=False)
        assert bad.status_code == 403
        ok = c.post(f"/incidents/{iid}/approvals/{aid}", data={"decision": "approve", "csrf": token, "comment": "ok"},
                    auth=ALICE, follow_redirects=False)
        assert ok.status_code == 303
        assert store.approvals(incident_id=iid)[0]["status"] == "Approved"
        # the viewer sees the page but no form
        assert 'name="csrf"' not in c.get(f"/incidents/{iid}", auth=VICTOR).text


def test_incident_api_filters(app, store):
    with TestClient(app) as c:
        ingest(c, app, am_alert("PodOOMKilled", namespace=NS, pod=POD))
        rows = c.get("/api/v1/incidents?severity=CRITICAL&cluster=test-eks", auth=VICTOR).json()
        assert len(rows) == 1 and rows[0]["incident_type"] == "OOMKilled"
        assert c.get("/api/v1/incidents?namespace=other", auth=VICTOR).json() == []
        detail = c.get(f"/api/v1/incidents/{rows[0]['id']}", auth=VICTOR).json()
        assert detail["report"]["requires_human_approval"] is True and detail["approvals"]


def test_metrics_endpoint(app):
    with TestClient(app) as c:
        ingest(c, app, am_alert("PodOOMKilled", namespace=NS, pod=POD))
        text = c.get("/metrics").text
        for name in ("sre_agent_alerts_received_total", "sre_agent_investigation_duration_seconds",
                     "sre_agent_incidents_total", "sre_agent_pending_approvals", "sre_agent_emails_sent_total",
                     "sre_agent_kubernetes_api_errors_total", "sre_agent_investigation_timeouts_total",
                     "sre_agent_ai_failures_total", "sre_agent_email_delivery_failures_total"):
            assert name in text, name


def test_rate_limit(settings, store, fake, smtp):
    settings.rate_limit_per_minute = 3
    app = create_app(settings, kube_factory=lambda n: fake, store=store, start_workers=False)
    with TestClient(app) as c:
        codes = [c.get("/api/v1/incidents", auth=ALICE).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:]


def test_no_users_configured_means_no_access(settings, store, fake, smtp):
    settings.approvers_file = None
    app = create_app(settings, kube_factory=lambda n: fake, store=store, start_workers=False)
    with TestClient(app) as c:
        assert c.get("/", auth=ALICE).status_code == 503
