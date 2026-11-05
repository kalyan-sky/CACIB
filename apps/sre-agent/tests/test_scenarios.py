"""The seven required scenarios, end to end through the pipeline against a fake cluster."""

from __future__ import annotations

import json

from fakes import NS, FakeKube, am_alert, deployment, event, node, pod

from sre_agent.analysis.llm import LlmAnalyzer
from sre_agent.config import LlmConfig
from sre_agent.models import INCONCLUSIVE, AmAlert, NormalizedAlert
from sre_agent.notify.email import EmailNotifier
from sre_agent.pipeline import Pipeline

POD = "payments-gateway-7d9f8c6b5d-x7k2p"


def run(settings, store, fake: FakeKube, **labels):
    pipeline = Pipeline(settings, store, lambda name: fake, EmailNotifier(settings, store),
                        LlmAnalyzer(None, LlmConfig()))
    alert = AmAlert.model_validate(am_alert(labels.pop("alertname", "KubePodCrashLooping"), **labels))
    na = NormalizedAlert.from_alert(alert, "test-eks", "prod", settings.critical_namespaces)
    incident_id, created = store.open_or_attach(cluster="test-eks", environment="prod", namespace=na.namespace,
                                                correlation_key=na.correlation_key, alertname=na.alertname,
                                                severity=na.severity, alert=na.model_dump())
    assert created
    report = pipeline.process(incident_id, na)
    return report, store.get(incident_id), pipeline


def commands_of(report, kind):
    return [c.command for s in report.resolution_steps for c in s.commands if c.kind == kind]


# ---------------------------------------------------------------------- Test 1: CrashLoopBackOff

def test_crashloop_detects_logs_events_cause_guide_email_approval(settings, store, smtp):
    fake = FakeKube(
        pods=[pod(POD, "payments-gateway", state={"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 40s"}},
                  last_state={"terminated": {"reason": "Error", "exitCode": 1, "finishedAt": "2026-11-05T10:02:00Z"}},
                  restarts=5)],
        deployments=[deployment("payments-gateway")],
        events=[event("Pod", POD, "BackOff", "Back-off restarting failed container app", count=12)],
        logs={f"{POD}/app": "starting payments-gateway\n",
              f"{POD}/app/previous": "2026-11-05T10:01:59Z starting payments-gateway\n"
                                     "2026-11-05T10:02:00Z FATAL: required config DB_URL is not set\n"},
    )
    report, inc, _ = run(settings, store, fake, alertname="KubePodCrashLooping", namespace=NS, pod=POD, container="app")

    # detected and collected
    assert report.incident_type == "CrashLoopBackOff"
    ops = [c[0] for c in fake.calls]
    assert ("read_pod_log", {"name": POD, "namespace": NS, "container": "app", "previous": True, "tail": 200}) in fake.calls
    assert "list_events" in ops and "read_deployment" in ops
    assert f"{POD}/app/previous" in inc["evidence"]["logs"]
    # cause from the log line, cited as evidence
    assert "missing" in report.root_cause and report.root_cause_status == "probable"
    assert any("DB_URL is not set" in e.observation and e.source == "logs.previous" for e in report.supporting_evidence)
    assert 0.5 < report.root_cause_confidence < 0.95
    # guide with labelled commands, nothing executed
    assert commands_of(report, "diagnostic") and commands_of(report, "verification")
    assert any("rollout undo" in c for c in commands_of(report, "remediation"))
    assert all(not c.executed_by_agent for c in report.commands_for_human_execution)
    assert report.requires_human_approval
    # email and approval
    assert len(smtp.sent) == 1 and inc["email_status"] == "sent"
    msg = smtp.sent[0]
    assert msg["Subject"] == f"[CRITICAL] Kubernetes Incident Detected - test-eks - {NS}"
    html = msg.get_body(("html",)).get_content()
    assert "DB_URL is not set" in html and "Proposed remediation command (NOT executed)" in html
    assert "head-sre@example.com" in msg["To"]  # prod:CRITICAL routing
    approvals = store.approvals(incident_id=report.incident_id)
    assert len(approvals) == 1 and approvals[0]["status"] == "Pending"


def test_crashloop_secret_values_never_leave_the_agent(settings, store, smtp):
    fake = FakeKube(
        pods=[pod(POD, "payments-gateway", state={"waiting": {"reason": "CrashLoopBackOff"}},
                  last_state={"terminated": {"reason": "Error", "exitCode": 1}}, restarts=3)],
        deployments=[deployment("payments-gateway")],
        logs={f"{POD}/app/previous": "connecting to postgresql://app:hunter2@db:5432/x\npassword=hunter2\n"
                                     "Authorization: Bearer abcdefghijklmnopqrstuvwxyz\nTraceback (most recent call last):\n"},
    )
    report, inc, _ = run(settings, store, fake, namespace=NS, pod=POD)
    blob = json.dumps(inc) + smtp.sent[0].get_body(("html",)).get_content()
    assert "hunter2" not in blob and "s3cr3t-value" not in blob and "abcdefghijklmnopqrstuvwxyz" not in blob
    assert "[REDACTED]" in blob


# ---------------------------------------------------------------------- Test 2: OOMKilled

def test_oomkilled(settings, store, smtp):
    fake = FakeKube(
        pods=[pod(POD, "payments-gateway", state={"waiting": {"reason": "CrashLoopBackOff"}},
                  last_state={"terminated": {"reason": "OOMKilled", "exitCode": 137}}, restarts=7, mem_limit="64Mi")],
        deployments=[deployment("payments-gateway", mem_limit="64Mi")],
        logs={f"{POD}/app/previous": "cache warming: 96 MB\n"},
        pod_metrics=[{"metadata": {"name": POD}, "containers": [{"name": "app", "usage": {"cpu": "20m", "memory": "61Mi"}}]}],
    )
    report, inc, _ = run(settings, store, fake, alertname="PodOOMKilled", namespace=NS, pod=POD)
    assert report.incident_type == "OOMKilled" and report.root_cause_status == "confirmed"
    assert "64Mi" in report.root_cause
    sources = {e.source for e in report.supporting_evidence}
    assert {"pod.status", "pod.spec", "metrics"} <= sources
    assert "pod_metrics" in [c[0] for c in fake.calls]
    assert any("set resources" in c for c in commands_of(report, "remediation"))
    assert any("leak" in f for f in report.possible_contributing_factors)  # hypothesis, not a claim
    assert any("top pod" in c for c in commands_of(report, "diagnostic"))


# ---------------------------------------------------------------------- Test 3: ImagePullBackOff

def test_image_pull_backoff(settings, store, smtp):
    image = "nginx:1.27-alpien"
    fake = FakeKube(
        pods=[pod(POD, "payments-gateway", image=image,
                  state={"waiting": {"reason": "ImagePullBackOff", "message": f'Back-off pulling image "{image}"'}})],
        deployments=[deployment("payments-gateway", image=image)],
        events=[event("Pod", POD, "Failed", f'Failed to pull image "{image}": rpc error: code = NotFound desc = '
                                            f'failed to resolve reference "docker.io/library/{image}": not found')],
    )
    report, _, _ = run(settings, store, fake, alertname="PodImagePullFailing", namespace=NS, pod=POD, severity="warning")
    assert report.incident_type == "ImagePullBackOff" and report.root_cause_status == "confirmed"
    assert image in report.root_cause and "does not exist" in report.root_cause
    assert report.severity == "HIGH"  # warning in a business-critical namespace
    assert any("set image" in c for c in commands_of(report, "remediation"))
    # no logs requested for a container that never started
    assert not any(op == "read_pod_log" for op, _ in fake.calls)


# ---------------------------------------------------------------------- Test 4: Pending

def test_pending_insufficient_cpu(settings, store, smtp):
    p = pod(POD, "payments-gateway", phase="Pending", node=None, cpu_request="64", ready=False)
    p["status"]["conditions"] = [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}]
    fake = FakeKube(
        pods=[p], deployments=[deployment("payments-gateway")],
        nodes=[node("ip-10-20-1-10"), node("ip-10-20-2-11")],
        events=[event("Pod", POD, "FailedScheduling",
                      "0/3 nodes are available: 3 Insufficient cpu. preemption: 0/3 nodes are available: "
                      "3 No preemption victims found for incoming pod.", component="default-scheduler")],
    )
    report, _, _ = run(settings, store, fake, alertname="PodPendingTooLong", namespace=NS, pod=POD, severity="warning")
    assert report.incident_type == "PodPending" and report.root_cause_status == "confirmed"
    assert "cpu=64" in report.root_cause
    assert any(e.source == "nodes" and "1930m" in e.observation for e in report.supporting_evidence)
    assert "list_nodes" in [c[0] for c in fake.calls]


def test_pending_untolerated_taint(settings, store, smtp):
    p = pod(POD, "payments-gateway", phase="Pending", node=None)
    fake = FakeKube(
        pods=[p], deployments=[deployment("payments-gateway")],
        nodes=[node("ip-10-20-1-10", taints=[{"key": "dedicated", "value": "payments", "effect": "NoSchedule"}])],
        events=[event("Pod", POD, "FailedScheduling", "0/3 nodes are available: 1 node(s) had untolerated taint "
                                                      "{dedicated: payments}, 2 node(s) didn't match Pod's node affinity/selector.")],
    )
    report, _, _ = run(settings, store, fake, alertname="PodPendingTooLong", namespace=NS, pod=POD)
    assert report.incident_type == "PodPending"
    assert "selector" in report.root_cause or "taint" in report.root_cause


# ---------------------------------------------------------------------- Test 5: Node NotReady

def test_node_not_ready(settings, store, smtp):
    n = node("ip-10-20-3-12", ready="Unknown", reason="NodeStatusUnknown", message="Kubelet stopped posting node status.")
    fake = FakeKube(nodes=[n, node("ip-10-20-1-10")],
                    pods=[pod("a-7d9f8c6b5d-aaaaa", "a", node="ip-10-20-3-12")],
                    events=[event("Node", "ip-10-20-3-12", "NodeNotReady", "Node ip-10-20-3-12 status is now: NodeNotReady",
                                  ns="default", component="node-controller")])
    report, _, _ = run(settings, store, fake, alertname="KubeNodeNotReady", node="ip-10-20-3-12", severity="warning")
    assert report.incident_type == "NodeNotReady"
    assert report.severity in ("HIGH", "CRITICAL")  # floor applied
    assert any(e.source == "node.conditions" and "Kubelet stopped posting" in e.observation for e in report.supporting_evidence)
    assert any("describe-instance-status" in c.command for c in report.recommended_investigations)
    assert report.root_cause_status == "probable"  # NotReady is confirmed; WHY is a hypothesis
    assert any("hypothesis" in f for f in report.possible_contributing_factors)
    assert ("read_node", {"name": "ip-10-20-3-12"}) in fake.calls
    assert any(op == "list_events" and kw.get("namespace") == "default" for op, kw in fake.calls)


# ---------------------------------------------------------------------- Test 6: insufficient evidence

def test_insufficient_evidence_is_inconclusive(settings, store, smtp):
    fake = FakeKube(pods=[pod(POD, "payments-gateway", ready=True)],
                    deployments=[deployment("payments-gateway", available=2)])
    report, _, _ = run(settings, store, fake, alertname="SomethingOdd", namespace=NS, pod=POD, severity="warning")
    assert report.root_cause_status == "inconclusive"
    assert report.root_cause.startswith(INCONCLUSIVE)
    assert report.root_cause_confidence <= 0.3
    assert report.recommended_investigations  # tells the engineer what to look at next
    assert not report.commands_for_human_execution  # no fix proposed without a cause
    assert store.approvals(incident_id=report.incident_id) == []


def test_missing_objects_do_not_invent_a_cause(settings, store, smtp):
    fake = FakeKube()  # the pod no longer exists
    report, inc, _ = run(settings, store, fake, alertname="KubePodCrashLooping", namespace=NS, pod=POD)
    assert report.root_cause_status == "inconclusive" and report.supporting_evidence == []
    assert any("NotFound" in e for e in inc["evidence"]["errors"])


# ---------------------------------------------------------------------- other classics

def test_create_container_config_error(settings, store, smtp):
    msg = "couldn't find key password in Secret payments/db-credentials"
    p = pod(POD, "billing", state={"waiting": {"reason": "CreateContainerConfigError", "message": msg}},
            env=[{"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "db-credentials", "key": "password"}}}])
    fake = FakeKube(pods=[p], deployments=[deployment("billing")],
                    events=[event("Pod", POD, "Failed", f"Error: {msg}")])
    report, _, _ = run(settings, store, fake, alertname="PodCreateContainerConfigError", namespace=NS, pod=POD)
    assert report.incident_type == "CreateContainerConfigError" and report.root_cause_status == "confirmed"
    assert "'password'" in report.root_cause and "db-credentials" in report.root_cause
    assert any("patch secret db-credentials" in c for c in commands_of(report, "remediation"))


def test_pvc_pending_missing_storage_class(settings, store, smtp):
    p = pod(POD, "docstore", phase="Pending", node=None,
            volumes=[{"name": "documents", "persistentVolumeClaim": {"claimName": "documents"}}])
    fake = FakeKube(pods=[p], deployments=[deployment("docstore")],
                    pvcs=[{"metadata": {"name": "documents", "namespace": NS},
                           "spec": {"storageClassName": "fast-ssd", "accessModes": ["ReadWriteOnce"],
                                    "resources": {"requests": {"storage": "1Gi"}}}, "status": {"phase": "Pending"}}],
                    storage_classes=[{"metadata": {"name": "gp3", "annotations": {
                        "storageclass.kubernetes.io/is-default-class": "true"}}}],
                    events=[event("PersistentVolumeClaim", "documents", "ProvisioningFailed",
                                  'storageclass.storage.k8s.io "fast-ssd" not found')])
    report, _, _ = run(settings, store, fake, alertname="PVCPending", namespace=NS, persistentvolumeclaim="documents")
    assert report.incident_type == "PersistentVolumeClaimPending" and "fast-ssd" in report.root_cause
    assert any("gp3 (default)" in e.observation for e in report.supporting_evidence)


def test_coredns_down_is_critical(settings, store, smtp):
    cd = deployment("coredns", ns="kube-system", replicas=0, available=0)
    fake = FakeKube(deployments=[cd])
    report, _, _ = run(settings, store, fake, alertname="CoreDNSDown", namespace="kube-system",
                       deployment="coredns", severity="warning")
    assert report.incident_type == "CoreDNSDown" and report.severity == "CRITICAL"


def test_rbac_forbidden_is_reported_not_fatal(settings, store, smtp):
    fake = FakeKube(pods=[pod(POD, "payments-gateway", state={"waiting": {"reason": "CrashLoopBackOff"}},
                              last_state={"terminated": {"reason": "OOMKilled", "exitCode": 137}}, restarts=2)],
                    deployments=[deployment("payments-gateway")], forbidden={"pod_metrics", "read_pod_log"})
    report, inc, _ = run(settings, store, fake, namespace=NS, pod=POD)
    assert report.incident_type == "OOMKilled"  # still diagnosed from status
    assert any("403" in e for e in inc["evidence"]["errors"])
    assert any("evidence gap" in n for n in report.analysis_notes)
