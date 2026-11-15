"""Similar past incidents, recurrence, and engineer feedback on the agent's accuracy."""

from __future__ import annotations

import re
import sqlite3

import pytest
from fakes import NS, FakeKube, am_alert, deployment, pod, webhook
from fastapi.testclient import TestClient
from test_api import ALICE, TOKEN, VICTOR, drain

from sre_agent.api import create_app
from sre_agent.store import Store

POD = "app-7d9f8c6b5d-x7k2p"


def oom_fake(dep="app", ns=NS):
    p = f"{dep}-7d9f8c6b5d-x7k2p"
    return FakeKube(pods=[pod(p, dep, ns=ns, state={"waiting": {"reason": "CrashLoopBackOff"}},
                              last_state={"terminated": {"reason": "OOMKilled", "exitCode": 137}}, restarts=4)],
                    deployments=[deployment(dep, ns=ns)])


@pytest.fixture
def world(settings, store, smtp):
    fakes = {"current": oom_fake()}
    app = create_app(settings, kube_factory=lambda n: fakes["current"], store=store, start_workers=False)
    return app, fakes, store, smtp


def incident(c, app, alertname="PodOOMKilled", ns=NS, pod_name=POD, resolve=True):
    out = c.post("/api/v1/alerts?cluster=test-eks", headers=TOKEN,
                 json=webhook(am_alert(alertname, namespace=ns, pod=pod_name))).json()
    drain(app)
    if resolve:
        c.post("/api/v1/alerts?cluster=test-eks", headers=TOKEN,
               json=webhook(am_alert(alertname, status="resolved", namespace=ns, pod=pod_name)))
        drain(app)
    return out["created"][0]


def sign_off(c, iid, verdict="correct", actions="Raised memory to 256Mi via MR !42", confirmation="", actual=""):
    page = c.get(f"/incidents/{iid}/rca", auth=ALICE).text
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    return c.post(f"/incidents/{iid}/rca", auth=ALICE, follow_redirects=False, data={
        "csrf": token, "action": "sign_off", "agent_verdict": verdict, "actions_taken": actions,
        "root_cause_confirmation": confirmation, "actual_cause_type": actual})


def test_new_incident_shows_what_fixed_it_last_time(world):
    app, fakes, store, smtp = world
    app.state.settings.email_dedup_minutes = 0  # the same problem twice within an hour is normally one email
    with TestClient(app) as c:
        first = incident(c, app)
        assert sign_off(c, first).status_code == 303
        second = incident(c, app, resolve=False)
        report = c.get(f"/api/v1/incidents/{second}", auth=VICTOR).json()["report"]
        page = c.get(f"/incidents/{second}", auth=VICTOR).text
    seen = report["similar_incidents"]
    assert [s["incident_id"] for s in seen] == [first]
    s = seen[0]
    assert s["fix"] == "Raised memory to 256Mi via MR !42" and s["agent_verdict"] == "correct" and s["signed_off"]
    assert s["root_cause_source"] == "agent, confirmed by engineer" and "same failure type" in s["match"]
    assert s["time_to_resolve_min"] is not None
    assert "Seen before" in page and first in page
    html = [m for m in smtp.sent if "Kubernetes Incident Detected" in m["Subject"]][-1].get_body(("html",)).get_content()
    assert "Seen before" in html and "Raised memory to 256Mi" in html


def test_engineer_correction_is_shown_next_time(world):
    app, fakes, store, smtp = world
    with TestClient(app) as c:
        first = incident(c, app)
        r = sign_off(c, first, verdict="incorrect", confirmation="Memory leak in the JSON parser after release 2.3",
                     actual="MemoryLeak", actions="Rolled back to 2.2")
        assert r.status_code == 303
        second = incident(c, app, resolve=False)
        report = c.get(f"/api/v1/incidents/{second}", auth=VICTOR).json()["report"]
    s = report["similar_incidents"][0]
    assert s["root_cause"] == "Memory leak in the JSON parser after release 2.3" and s["root_cause_source"] == "engineer"
    assert any("corrected the agent" in n for n in report["analysis_notes"])


def test_unrelated_incidents_are_not_suggested(world):
    app, fakes, store, smtp = world
    with TestClient(app) as c:
        incident(c, app)  # OOM on app in payments
        fakes["current"] = oom_fake(dep="ledger", ns="core-banking")  # OOM, other workload, other namespace
        other = incident(c, app, ns="core-banking", pod_name="ledger-7d9f8c6b5d-x7k2p", resolve=False)
        report = c.get(f"/api/v1/incidents/{other}", auth=VICTOR).json()["report"]
    # same type elsewhere is still useful (score: type 3 + alert 1 = 4), but nothing unrelated
    assert all(s["incident_type"] == "OOMKilled" for s in report["similar_incidents"])
    fakes["current"] = FakeKube()  # an inconclusive incident: no history to suggest
    with TestClient(app) as c:
        odd = incident(c, app, alertname="SomethingOdd", ns="data", pod_name="x-7d9f8c6b5d-x7k2p", resolve=False)
        assert c.get(f"/api/v1/incidents/{odd}", auth=VICTOR).json()["report"]["similar_incidents"] == []


def test_recurrence_flagged(world):
    app, fakes, store, smtp = world
    with TestClient(app) as c:
        for _ in range(3):
            incident(c, app)
        latest = incident(c, app, resolve=False)
        report = c.get(f"/api/v1/incidents/{latest}", auth=VICTOR).json()["report"]
    assert report["recurrences_30d"] == 3
    assert any(n.startswith("Recurring: 3 earlier OOMKilled") for n in report["analysis_notes"])
    assert len(report["similar_incidents"]) == 3


def test_sign_off_requires_a_verdict(world):
    app, fakes, store, smtp = world
    with TestClient(app) as c:
        iid = incident(c, app)
        assert sign_off(c, iid, verdict="").status_code == 409
        # incorrect without the real cause is refused too
        assert sign_off(c, iid, verdict="incorrect", confirmation="").status_code == 409
        assert sign_off(c, iid, verdict="partial").status_code == 303


def test_accuracy_scorecard_page_api_and_metrics(world):
    app, fakes, store, smtp = world
    with TestClient(app) as c:
        verdicts = ["correct", "correct", "partial", "incorrect"]
        for v in verdicts:
            iid = incident(c, app)
            sign_off(c, iid, verdict=v, confirmation="real cause: leak" if v == "incorrect" else "")
        acc = c.get("/api/v1/accuracy", auth=VICTOR).json()
        page = c.get("/accuracy", auth=VICTOR)
        home = c.get("/", auth=VICTOR).text
        prom = c.get("/metrics").text
    o = acc["overall"]
    assert (o["reviewed"], o["correct"], o["partial"], o["incorrect"]) == (4, 2, 1, 1)
    assert o["accuracy"] == 0.625  # (2 + 0.5) / 4
    assert acc["by_type"]["OOMKilled"]["accuracy"] == 0.625
    assert acc["avg_confidence"]["correct"] == 0.9
    assert page.status_code == 200 and "62%" in page.text and "OOMKilled" in page.text
    assert "Agent accuracy (4 reviewed)" in home
    assert 'sre_agent_diagnosis_accuracy{incident_type="OOMKilled"} 0.625' in prom
    assert 'sre_agent_diagnosis_feedback_total{incident_type="OOMKilled",verdict="incorrect"}' in prom


def test_rca_shows_verdict_and_related(world):
    app, fakes, store, smtp = world
    with TestClient(app) as c:
        first = incident(c, app)
        sign_off(c, first)
        second = incident(c, app)
        sign_off(c, second, verdict="partial")
        md = c.get(f"/incidents/{second}/rca.md", auth=VICTOR).text
    assert "Agent diagnosis reviewed by the engineer: **partial**" in md
    assert "## Related incidents (seen before)" in md and first in md


def test_old_database_is_migrated(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE rca_notes (incident_id TEXT PRIMARY KEY, actions_taken TEXT NOT NULL DEFAULT '', "
                "root_cause_confirmation TEXT NOT NULL DEFAULT '', lessons TEXT NOT NULL DEFAULT '', "
                "follow_ups TEXT NOT NULL DEFAULT '', updated_by TEXT, updated_at TEXT, signed_off_by TEXT, "
                "signed_off_at TEXT)")
    con.execute("INSERT INTO rca_notes (incident_id, actions_taken) VALUES ('INC-0001', 'kept')")
    con.commit()
    con.close()
    s = Store(str(db))
    notes = s.rca_notes("INC-0001")
    assert notes["actions_taken"] == "kept" and "agent_verdict" in notes
