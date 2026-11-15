"""RCA: format, honesty labels, timeline, engineer sign-off (HITL), emails, runbook coverage."""

from __future__ import annotations

import re

import pytest
from fakes import NS, FakeKube, am_alert, deployment, event, pod, webhook
from fastapi.testclient import TestClient
from test_api import ALICE, TOKEN, VICTOR, drain

from sre_agent.analysis.runbooks import RUNBOOKS
from sre_agent.api import create_app
from sre_agent.models import INCONCLUSIVE

POD = "app-7d9f8c6b5d-x7k2p"


@pytest.fixture
def app(settings, store, smtp):
    fake = FakeKube(pods=[pod(POD, "app", state={"waiting": {"reason": "CrashLoopBackOff"}},
                              last_state={"terminated": {"reason": "OOMKilled", "exitCode": 137}}, restarts=4)],
                    deployments=[deployment("app")],
                    events=[event("Pod", POD, "BackOff", "Back-off restarting failed container app", count=9)])
    return create_app(settings, kube_factory=lambda name: fake, store=store, start_workers=False)


def fire_and_resolve(c, app):
    out = c.post("/api/v1/alerts?cluster=test-eks", headers=TOKEN,
                 json=webhook(am_alert("PodOOMKilled", namespace=NS, pod=POD))).json()
    drain(app)
    c.post("/api/v1/alerts?cluster=test-eks", headers=TOKEN,
           json=webhook(am_alert("PodOOMKilled", status="resolved", namespace=NS, pod=POD)))
    drain(app)
    return out["created"][0]


def test_rca_document_has_every_section(app, smtp):
    with TestClient(app) as c:
        iid = fire_and_resolve(c, app)
        rca = c.get(f"/api/v1/incidents/{iid}/rca", auth=VICTOR).json()
        md = c.get(f"/incidents/{iid}/rca.md", auth=VICTOR).text
    assert rca["status"] == "Resolved - awaiting review and sign-off"
    assert rca["root_cause_status"] == "confirmed" and "memory" in rca["root_cause"]
    assert rca["duration"] != "unknown" and rca["resolved_at"]
    events = [t["event"] for t in rca["timeline"]]
    assert any("started firing" in e for e in events) and any("investigation completed" in e for e in events)
    assert any("resolved" in e for e in events) and any("BackOff" in e for e in events)
    assert [w["basis"] for w in rca["five_whys"]][:3] == ["evidence", "evidence", "evidence"]
    assert any(w["basis"] == "hypothesis" for w in rca["five_whys"])  # contributing factors are not presented as fact
    assert {a["kind"] for a in rca["capa"]} == {"corrective", "preventive", "detective"}
    assert rca["owner_team"] == "team-payments"
    for heading in ("## 1. Summary", "## 2. Impact", "## 3. Timeline", "## 4. Detection", "## 5. Root cause",
                    "### 5 whys", "## 6. Resolution", "## 7. Corrective and preventive actions",
                    "## 8. Lessons learned", "## 9. Open questions", "read-only commands"):
        assert heading in md, heading
    assert "Not yet recorded by an engineer" in md


def test_rca_emailed_once_on_resolution_with_markdown_attachment(app, smtp):
    with TestClient(app) as c:
        iid = fire_and_resolve(c, app)
        app.state.pipeline.send_rca(iid)  # a second resolution notice must not resend
    rcas = [m for m in smtp.sent if "RCA FOR REVIEW" in m["Subject"]]
    assert len(rcas) == 1 and iid in rcas[0]["Subject"]
    attachment = next(rcas[0].iter_attachments())
    assert attachment.get_filename() == f"RCA-{iid}.md" and "## 5. Root cause" in str(attachment.get_content())


def test_engineer_completes_and_signs_off(app, store, smtp):
    with TestClient(app) as c:
        iid = fire_and_resolve(c, app)
        page = c.get(f"/incidents/{iid}/rca", auth=ALICE)
        token = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
        assert c.post(f"/incidents/{iid}/rca", data={"csrf": "forged", "action": "save"}, auth=ALICE,
                      follow_redirects=False).status_code == 403
        assert c.post(f"/incidents/{iid}/rca", data={"csrf": token, "action": "save"}, auth=VICTOR,
                      follow_redirects=False).status_code == 403  # viewers cannot write the RCA
        # sign-off needs the actions taken
        assert c.post(f"/incidents/{iid}/rca", data={"csrf": token, "action": "sign_off"}, auth=ALICE).status_code == 409
        r = c.post(f"/incidents/{iid}/rca", auth=ALICE, follow_redirects=False, data={
            "csrf": token, "action": "sign_off", "actions_taken": "Raised memory limit to 256Mi via MR !42",
            "root_cause_confirmation": "Confirmed: cache is 96 MB, limit was 64Mi",
            "lessons": "Size limits from measured usage\n- Alert on 90% memory", "follow_ups": "Add memory load test"})
        assert r.status_code == 303
        rca = c.get(f"/api/v1/incidents/{iid}/rca", auth=VICTOR).json()
        # once final, it can no longer be edited
        assert c.post(f"/incidents/{iid}/rca", data={"csrf": token, "action": "save"}, auth=ALICE).status_code == 409
    assert rca["status"] == "Final" and rca["signed_off"].startswith("alice")
    assert rca["actions_taken"].startswith("Raised memory") and "Size limits from measured usage" in rca["lessons"]
    assert any(a["action"] == "Add memory load test" for a in rca["capa"])
    assert any("signed off" in t["event"] for t in rca["timeline"])
    assert len([m for m in smtp.sent if "FINAL RCA" in m["Subject"]]) == 1
    assert {"rca.updated", "rca.signed_off", "rca.emailed.final"} <= {a["action"] for a in store.audit_log(iid)}


def test_alert_without_workload_uses_runbook_not_a_guess(settings, store, smtp):
    app = create_app(settings, kube_factory=lambda n: FakeKube(), store=store, start_workers=False)
    with TestClient(app) as c:
        out = c.post("/api/v1/alerts?cluster=test-eks", headers=TOKEN, json=webhook(
            am_alert("AwsCniIpPoolLow", severity="warning"))).json()
        drain(app)
        report = c.get(f"/api/v1/incidents/{out['created'][0]}", auth=VICTOR).json()["report"]
    assert report["incident_type"] == "IpExhaustion"
    assert report["root_cause"].startswith(INCONCLUSIVE) and report["root_cause_status"] == "inconclusive"
    assert any("Prefix delegation" in f and "(hypothesis)" in f for f in report["possible_contributing_factors"])
    assert any("describe-subnets" in cmd["command"] for cmd in report["recommended_investigations"])
    assert report["commands_for_human_execution"] == []  # no fix proposed without a cause


def test_runbooks_are_read_only():
    for name, rb in RUNBOOKS.items():
        for cmd, _ in rb.checks:
            assert not re.search(r"\b(delete|apply|patch|edit|scale|rollout (undo|restart)|set |cordon|drain|"
                                 r"terminate|create)\b", cmd), f"{name}: {cmd}"


def test_resolution_during_investigation_keeps_resolved_and_sends_rca(app, store, smtp):
    with TestClient(app) as c:
        out = c.post("/api/v1/alerts?cluster=test-eks", headers=TOKEN,
                     json=webhook(am_alert("PodOOMKilled", namespace=NS, pod=POD))).json()
        iid = out["created"][0]
        job = app.state.pipeline.queue.get_nowait()
        c.post("/api/v1/alerts?cluster=test-eks", headers=TOKEN,  # resolves before the worker runs
               json=webhook(am_alert("PodOOMKilled", status="resolved", namespace=NS, pod=POD)))
        drain(app)  # the RCA job finds no report yet
        app.state.pipeline.process(iid, job[2])
    assert store.get(iid)["status"] == "resolved"
    assert len([m for m in smtp.sent if "RCA FOR REVIEW" in m["Subject"]]) == 1
