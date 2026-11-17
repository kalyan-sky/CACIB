"""The shared incident layout: headline, at-a-glance view, track record, and the same sections everywhere."""

from __future__ import annotations

import re

from fakes import NS, FakeKube, am_alert, deployment, pod, webhook
from fastapi.testclient import TestClient
from test_api import ALICE, TOKEN, VICTOR, drain

from sre_agent import presentation
from sre_agent.api import create_app
from sre_agent.models import IncidentReport

SECTIONS = ["1 · WHAT HAPPENED", "2 · ROOT CAUSE", "3 · EVIDENCE (OBSERVED)", "4 · SEEN BEFORE", "5 · WHAT TO DO",
            "6 · RISK AND PREREQUISITES"]


def report(**kw):
    base = dict(incident_id="INC-0001", severity="CRITICAL", cluster="bank-sim", namespace="payments",
                affected_resources=["deployment/payments-gateway", "pod/payments-gateway-x"], incident_type="OOMKilled",
                root_cause="limit too low", root_cause_status="confirmed", root_cause_confidence=0.9)
    return IncidentReport(**{**base, **kw})


def test_headlines_are_plain_english():
    assert presentation.headline(report()) == "payments-gateway keeps getting killed: it needs more memory than its limit"
    assert presentation.headline(report(incident_type="NodeNotReady", affected_resources=["node/ip-10-0-1-5"])) \
        == "Node ip-10-0-1-5 is down: its pods will be moved"
    assert presentation.headline(report(incident_type="unknown", incident_summary="Something odd. More text")) \
        == "Something odd"


def test_track_record_and_confidence_words():
    assert presentation.track_record_text(report()) == \
        "OOMKilled: not measured yet (no signed-off RCAs of this type)"
    r = report(track_record={"reviewed": 5, "correct": 4, "partial": 1, "incorrect": 0})
    assert presentation.track_record_text(r) == \
        "OOMKilled: agent confirmed correct in 4 of 5 signed-off RCAs (1 partly)"
    assert [presentation.confidence_word(c) for c in (0.95, 0.7, 0.2)] == ["High", "Medium", "Low"]
    v = presentation.view(report(), {"environment": "prod", "status": "open",
                                     "evidence": {"duration_seconds": 2.5}}, "Pending", "https://agent.example")
    r_fix = report(commands_for_human_execution=[{"kind": "remediation", "command": "kubectl -n p set resources x"}])
    v_fix = presentation.view(r_fix, {"environment": "prod", "status": "open", "evidence": {"duration_seconds": 2.5}},
                              "Pending")
    assert v_fix["status_line"] == "Open · investigated in 2.5 s · fix awaiting approval"
    assert v["status_line"] == "Open · investigated in 2.5 s · no change needed from the agent's side"
    assert v["where"] == "bank-sim (prod) › payments › deployment/payments-gateway"
    assert v["links"]["rca"] == "https://agent.example/incidents/INC-0001/rca"


def oom_app(settings, store):
    fake = FakeKube(pods=[pod("app-7d9f8c6b5d-x7k2p", "app", state={"waiting": {"reason": "CrashLoopBackOff"}},
                              last_state={"terminated": {"reason": "OOMKilled", "exitCode": 137}}, restarts=4)],
                    deployments=[deployment("app")])
    return create_app(settings, kube_factory=lambda n: fake, store=store, start_workers=False)


def test_email_dashboard_and_rca_share_the_layout(settings, store, smtp):
    settings.dashboard_url = "https://agent.example"
    app = oom_app(settings, store)
    with TestClient(app) as c:
        out = c.post("/api/v1/alerts?cluster=test-eks", headers=TOKEN,
                     json=webhook(am_alert("PodOOMKilled", namespace=NS, pod="app-7d9f8c6b5d-x7k2p"))).json()
        drain(app)
        iid = out["created"][0]
        page = c.get(f"/incidents/{iid}", auth=VICTOR).text
        c.post("/api/v1/alerts?cluster=test-eks", headers=TOKEN, json=webhook(
            am_alert("PodOOMKilled", status="resolved", namespace=NS, pod="app-7d9f8c6b5d-x7k2p")))
        drain(app)
    email = smtp.sent[0].get_body(("html",)).get_content()
    text = smtp.sent[0].get_body(("plain",)).get_content()
    rca = next(m for m in smtp.sent if "RCA FOR REVIEW" in m["Subject"]).get_body(("html",)).get_content()
    headline = "app keeps getting killed: it needs more memory than its limit"
    for doc in (email, page, rca, text):
        assert headline in doc
    for i, s in enumerate(SECTIONS):  # same numbered sections, same order, in the email and the dashboard
        assert s in email and s in text
        assert s.split(" · ")[1].lower() in page.lower()
    order = [email.index(s) for s in SECTIONS]
    assert order == sorted(order)
    assert 'href="https://agent.example/incidents/' in email  # real buttons when a dashboard URL exists
    assert "Track record" in email and "not measured yet" in email
    assert re.search(r"badge-(check|fix|verify|rollback)", page) and "Download fix script" in page
    assert "ROOT CAUSE ANALYSIS · FOR REVIEW" in rca and "5 · ROOT CAUSE" in rca and "7 · CORRECTIVE" in rca
