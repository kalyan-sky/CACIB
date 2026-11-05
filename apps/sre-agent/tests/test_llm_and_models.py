"""LLM refinement is validated, grounded and fails safe; alert normalisation; config parsing."""

from __future__ import annotations

import json

from fakes import NS, FakeKube, deployment, pod

from sre_agent.analysis.llm import LlmAnalyzer, make_provider
from sre_agent.analysis.rules import build_report
from sre_agent.config import Limits, LlmConfig, Settings
from sre_agent.diagnostics import DiagnosticEngine
from sre_agent.models import AmAlert, NormalizedAlert, map_severity

POD = "app-7d9f8c6b5d-x7k2p"


def evidence_and_draft():
    fake = FakeKube(pods=[pod(POD, "app", state={"waiting": {"reason": "CrashLoopBackOff"}},
                              last_state={"terminated": {"reason": "OOMKilled", "exitCode": 137}}, restarts=4)],
                    deployments=[deployment("app")])
    alert = NormalizedAlert.from_alert(AmAlert(status="firing", labels={"alertname": "PodOOMKilled", "namespace": NS,
                                                                        "pod": POD, "severity": "critical"}),
                                       "test-eks", "prod", [])
    ev = DiagnosticEngine(fake, Limits()).investigate(alert)
    return ev, build_report(alert, ev, "INC-0042")


class StubProvider:
    name = "stub"

    def __init__(self, answer: str | Exception):
        self.answer = answer

    def complete(self, system: str, user: str) -> str:
        assert "Never invent" in system and "EVIDENCE" in user
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_no_provider_by_default():
    assert make_provider(LlmConfig()) is None


def test_llm_answer_is_validated_and_ungrounded_evidence_dropped():
    ev, draft = evidence_and_draft()
    answer = draft.model_dump()
    answer.update({
        "incident_id": "INC-9999", "cluster": "other-cluster", "requires_human_approval": False,
        "root_cause": "Memory limit too low for the cache.", "root_cause_confidence": 0.95,
        "supporting_evidence": [
            {"source": "pod.status", "observation": "container app last terminated: OOMKilled, exit code 137"},
            {"source": "logs.previous", "observation": "java.lang.OutOfMemoryError in CacheLoader thread pool"}],
        "agent_actions_taken": ["kubectl delete pod everything"],
    })
    report = LlmAnalyzer(StubProvider("Here you go:\n" + json.dumps(answer)), LlmConfig(provider="stub")).refine(draft, ev)
    assert report.analysis_engine == "llm:stub"
    assert report.incident_id == "INC-0042" and report.cluster == "test-eks"  # identity can't be changed
    assert report.requires_human_approval is True
    assert report.agent_actions_taken == draft.agent_actions_taken  # can't claim actions
    assert len(report.supporting_evidence) == 1  # the invented log line is dropped
    assert any("dropped" in n for n in report.analysis_notes)
    assert report.root_cause_confidence <= draft.root_cause_confidence


def test_llm_failure_keeps_rule_report():
    ev, draft = evidence_and_draft()
    for bad in (TimeoutError("slow"), "not json at all", json.dumps({"severity": "APOCALYPTIC"})):
        report = LlmAnalyzer(StubProvider(bad), LlmConfig(provider="stub")).refine(draft.model_copy(deep=True), ev)
        assert report.analysis_engine == "rules" and report.incident_type == "OOMKilled"
        assert any("LLM analysis failed" in n for n in report.analysis_notes)


def test_severity_mapping():
    assert map_severity({"severity": "critical"}, []) == "CRITICAL"
    assert map_severity({"severity": "warning", "namespace": "payments"}, ["payments"]) == "HIGH"
    assert map_severity({"severity": "warning", "namespace": "data"}, ["payments"]) == "MEDIUM"
    assert map_severity({"severity": "info"}, []) == "INFO"
    assert map_severity({"severity": "warning", "sre_severity": "low"}, []) == "LOW"


def test_workload_and_correlation_keys():
    def na(**labels):
        return NormalizedAlert.from_alert(AmAlert(status="firing", labels={"alertname": "A", **labels}), "c", "dev", [])

    assert na(namespace="p", pod="payments-gateway-7d9f8c6b5d-x7k2p").workload == "payments-gateway"
    assert na(namespace="p", pod="node-exporter-x7k2p").workload == "node-exporter"
    assert na(namespace="p", deployment="x").correlation_key == "c|workload/p/x"
    assert na(node="ip-10-0-0-1").correlation_key == "c|node/ip-10-0-0-1"
    assert na(namespace="p", pod="../../etc").pod is None  # label values must be valid names


def test_settings_from_env(monkeypatch, tmp_path):
    secret = tmp_path / "smtp"
    secret.write_text("app-password\n")
    clusters = tmp_path / "clusters.yaml"
    clusters.write_text("clusters:\n  - {name: prod-eks, environment: prod, in_cluster: true}\n"
                        "  - {name: uat-eks, environment: uat, kubeconfig: /etc/k/uat, context: uat}\n")
    monkeypatch.setenv("SRE_AGENT_CLUSTERS_FILE", str(clusters))
    monkeypatch.setenv("SRE_AGENT_SMTP_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("SRE_AGENT_EMAIL_TO", "team@example.com")
    monkeypatch.setenv("SRE_AGENT_EMAIL_TO_CRITICAL", "oncall@example.com")
    monkeypatch.setenv("SRE_AGENT_EMAIL_TO_PROD_CRITICAL", "oncall@example.com,head@example.com")
    s = Settings.from_env()
    assert set(s.clusters) == {"prod-eks", "uat-eks"} and s.default_cluster is None
    assert s.clusters["uat-eks"].kubeconfig == "/etc/k/uat"
    assert s.smtp.password == "app-password"
    assert s.recipients_for("prod", "CRITICAL") == ["oncall@example.com", "head@example.com"]
    assert s.recipients_for("uat", "CRITICAL") == ["oncall@example.com"]
    assert s.recipients_for("uat", "LOW") == ["team@example.com"]
