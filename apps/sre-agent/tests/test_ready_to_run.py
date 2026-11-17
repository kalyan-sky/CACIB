"""Ready-to-run commands: values filled from evidence, no <placeholders>, and fix scripts that are safe."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from fakes import NS, FakeKube, deployment, event, node, pod
from test_scenarios import POD, commands_of, run

from sre_agent.fixscript import build_script
from sre_agent.store import Store

PLACEHOLDER = re.compile(r"<[A-Za-z][^<>]*>")


def assert_ready(report):
    for step in report.resolution_steps:
        for c in step.commands:
            if c.command.startswith("#"):
                continue
            body = c.command.replace("</dev/null", "").replace("<<'EOF'", "")
            assert not PLACEHOLDER.search(body), f"placeholder left: {c.command}"
            assert subprocess.run(["bash", "-n", "-c", c.command]).returncode == 0, c.command


def oom_case():
    return FakeKube(
        pods=[pod(POD, "payments-gateway", state={"waiting": {"reason": "CrashLoopBackOff"}},
                  last_state={"terminated": {"reason": "OOMKilled", "exitCode": 137}}, restarts=7, mem_limit="64Mi")],
        deployments=[deployment("payments-gateway", mem_limit="64Mi")],
        pod_metrics=[{"metadata": {"name": POD}, "containers": [{"name": "app", "usage": {"memory": "61Mi"}}]}])


def test_oom_memory_is_computed_from_evidence(settings, store, smtp):
    report, _, _ = run(settings, store, oom_case(), alertname="PodOOMKilled", namespace=NS, pod=POD)
    fix = commands_of(report, "remediation")[0]
    # limit = max(2 x 64Mi, 1.5 x 61Mi) = 128Mi; request = max(1.2 x 61Mi, 64Mi, 48Mi) -> 73.2Mi -> 96Mi
    assert "--requests=memory=96Mi --limits=memory=128Mi" in fix
    assert_ready(report)


def test_image_pull_uses_previous_working_image(settings, store, smtp):
    bad = "registry.example/app:1.1-typo"
    fake = FakeKube(
        pods=[pod(POD, "payments-gateway", image=bad, state={"waiting": {"reason": "ImagePullBackOff"}})],
        deployments=[deployment("payments-gateway", image=bad)],
        events=[event("Pod", POD, "Failed", f'Failed to pull image "{bad}": not found')],
        replicasets=[
            {"metadata": {"name": "payments-gateway-new", "annotations": {"deployment.kubernetes.io/revision": "4"}},
             "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "app", "image": bad}]}}}, "status": {}},
            {"metadata": {"name": "payments-gateway-old", "annotations": {"deployment.kubernetes.io/revision": "3"}},
             "spec": {"replicas": 2, "template": {"spec": {"containers": [{"name": "app", "image": "registry.example/app:1.0"}]}}},
             "status": {"readyReplicas": 2}}])
    report, _, _ = run(settings, store, fake, alertname="PodImagePullFailing", namespace=NS, pod=POD)
    assert commands_of(report, "remediation") == [
        f"kubectl -n {NS} set image deployment/payments-gateway app=registry.example/app:1.0"]
    assert_ready(report)


def test_first_deploy_image_asks_instead_of_placeholder(settings, store, smtp):
    image = "nginx:1.27-alpien"
    fake = FakeKube(pods=[pod(POD, "web", image=image, state={"waiting": {"reason": "ImagePullBackOff"}})],
                    deployments=[deployment("web", image=image)],
                    events=[event("Pod", POD, "Failed", "manifest unknown")])
    report, _, _ = run(settings, store, fake, alertname="PodImagePullFailing", namespace=NS, pod=POD)
    fix = commands_of(report, "remediation")[0]
    assert fix.startswith('read -rp "Correct image') and '"$NEW_IMAGE"' in fix
    assert_ready(report)


def test_taint_gets_a_concrete_toleration(settings, store, smtp):
    p = pod(POD, "payments-worker", phase="Pending", node=None, node_selector={"team": "payments"})
    fake = FakeKube(pods=[p], deployments=[deployment("payments-worker")],
                    nodes=[node("ip-10-20-1-10", taints=[{"key": "dedicated", "value": "payments", "effect": "NoSchedule"}],
                                labels={"team": "payments"}), node("ip-10-20-2-11")],
                    events=[event("Pod", POD, "FailedScheduling", "0/2 nodes are available: 1 node(s) had untolerated "
                                  "taint {dedicated: payments}, 1 node(s) didn't match Pod's node affinity/selector.")])
    report, _, _ = run(settings, store, fake, alertname="PodPendingTooLong", namespace=NS, pod=POD)
    fixes = commands_of(report, "remediation")
    assert len(fixes) == 1  # the selector is right (it finds the dedicated node); only the toleration is missing
    assert '"key":"dedicated","operator":"Equal","value":"payments","effect":"NoSchedule"' in fixes[0]
    assert_ready(report)


def test_pending_cpu_default_fits_a_node(settings, store, smtp):
    p = pod(POD, "reports", phase="Pending", node=None, cpu_request="64")
    fake = FakeKube(pods=[p], deployments=[deployment("reports")], nodes=[node("ip-10-20-1-10", cpu="1930m")],
                    events=[event("Pod", POD, "FailedScheduling", "0/3 nodes are available: 3 Insufficient cpu.")])
    report, _, _ = run(settings, store, fake, alertname="PodPendingTooLong", namespace=NS, pod=POD)
    assert "[490m]" in commands_of(report, "remediation")[0]  # a quarter of 1930m, rounded up
    assert_ready(report)


def test_missing_secret_key_is_prompted_and_never_echoed(settings, store, smtp):
    msg = "couldn't find key password in Secret payments/db-credentials"
    fake = FakeKube(pods=[pod(POD, "billing", state={"waiting": {"reason": "CreateContainerConfigError", "message": msg}})],
                    deployments=[deployment("billing")])
    report, _, _ = run(settings, store, fake, alertname="PodCreateContainerConfigError", namespace=NS, pod=POD)
    fix = commands_of(report, "remediation")[0]
    assert fix.startswith("read -rsp") and "base64" in fix and "unset VALUE" in fix and "/data/password" in fix
    assert_ready(report)


def test_pvc_recreated_from_evidence(settings, store, smtp):
    p = pod(POD, "docstore", phase="Pending", node=None,
            volumes=[{"name": "d", "persistentVolumeClaim": {"claimName": "documents"}}])
    fake = FakeKube(pods=[p], deployments=[deployment("docstore")],
                    pvcs=[{"metadata": {"name": "documents", "namespace": NS},
                           "spec": {"storageClassName": "fast-ssd", "accessModes": ["ReadWriteOnce"],
                                    "resources": {"requests": {"storage": "5Gi"}}}, "status": {"phase": "Pending"}}],
                    storage_classes=[{"metadata": {"name": "gp3", "annotations": {
                        "storageclass.kubernetes.io/is-default-class": "true"}}}],
                    events=[event("PersistentVolumeClaim", "documents", "ProvisioningFailed",
                                  'storageclass.storage.k8s.io "fast-ssd" not found')])
    report, _, _ = run(settings, store, fake, alertname="PVCPending", namespace=NS, persistentvolumeclaim="documents")
    fixes = "\n".join(commands_of(report, "remediation"))
    assert "storageClassName: gp3" in fixes and "storage: 5Gi" in fixes and "= Pending &&" in fixes
    assert_ready(report)


# ---------------------------------------------------------------------- the fix script, executed

@pytest.fixture
def stub_kubectl(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "kubectl.log"
    (bindir / "kubectl").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1 $2" = "config current-context" ]; then echo "${STUB_CONTEXT}"; exit 0; fi\n'
        f'echo "$*" >> {log}\n')
    (bindir / "kubectl").chmod(0o755)
    return bindir, log


def run_script(script: str, tmp_path: Path, stub, *, answers: str, context: str = "eks-test-eks", mode: str = ""):
    bindir, log = stub
    f = tmp_path / "fix.sh"
    f.write_text(script)
    tty = tmp_path / "answers"
    tty.write_text(answers)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "STUB_CONTEXT": context, "FIX_TTY": str(tty), "HOME": str(tmp_path)}
    r = subprocess.run(["bash", str(f), *([mode] if mode else [])], env=env, capture_output=True, text=True, timeout=30)
    return r, (log.read_text().splitlines() if log.exists() else [])


def oom_script(settings, store):
    report, _, _ = run(settings, store, oom_case(), alertname="PodOOMKilled", namespace=NS, pod=POD)
    return build_script(report, "Approved")


def test_script_is_valid_bash(settings, store, smtp):
    assert subprocess.run(["bash", "-n", "-c", oom_script(settings, store)]).returncode == 0


def test_script_runs_checks_fix_and_verify_in_order(settings, store, smtp, tmp_path, stub_kubectl):
    r, calls = run_script(oom_script(settings, store), tmp_path, stub_kubectl, answers="y\n")
    assert r.returncode == 0, r.stderr + r.stdout
    first_change = next(i for i, c in enumerate(calls) if "set resources" in c)
    assert any("describe pod" in c for c in calls[:first_change])  # checks first
    assert any("rollout status" in c for c in calls[first_change:])  # verification after
    assert "--limits=memory=128Mi" in calls[first_change]


def test_script_refuses_the_wrong_cluster(settings, store, smtp, tmp_path, stub_kubectl):
    r, calls = run_script(oom_script(settings, store), tmp_path, stub_kubectl, answers="y\n", context="eks-prod-other")
    assert r.returncode == 2 and "Refusing" in r.stderr and calls == []


def test_script_stops_when_the_engineer_says_no(settings, store, smtp, tmp_path, stub_kubectl):
    r, calls = run_script(oom_script(settings, store), tmp_path, stub_kubectl, answers="n\n")
    assert r.returncode == 1 and "Stopped before the change" in r.stdout
    assert not any("set resources" in c for c in calls)


def test_script_check_mode_changes_nothing(settings, store, smtp, tmp_path, stub_kubectl):
    r, calls = run_script(oom_script(settings, store), tmp_path, stub_kubectl, answers="", mode="--check")
    assert r.returncode == 0 and calls and not any("set resources" in c or "rollout undo" in c for c in calls)


def test_script_rollback_mode(settings, store, smtp, tmp_path, stub_kubectl):
    r, calls = run_script(oom_script(settings, store), tmp_path, stub_kubectl, answers="y\n", mode="--rollback")
    assert r.returncode == 0 and any("rollout undo deployment/payments-gateway" in c for c in calls)
    assert not any("set resources" in c for c in calls)


def test_inconclusive_script_proposes_nothing(settings, smtp):
    fake = FakeKube(pods=[pod(POD, "payments-gateway", ready=True)], deployments=[deployment("payments-gateway", available=2)])
    report, _, _ = run(settings, Store(":memory:"), fake, alertname="SomethingOdd", namespace=NS, pod=POD)
    script = build_script(report, "not required")
    assert "No fix is proposed" in script and "change '" not in script


def test_email_and_dashboard_carry_the_ready_commands(settings, store, smtp):
    report, inc, pipeline = run(settings, store, oom_case(), alertname="PodOOMKilled", namespace=NS, pod=POD)
    msg = smtp.sent[0]
    html = msg.get_body(("html",)).get_content()
    assert "Fastest:" in html and "bash fix.sh" in html and "--limits=memory=128Mi" in html
    att = [a for a in msg.iter_attachments() if a.get_filename() == f"fix-{report.incident_id}.sh.txt"]
    assert att and "set -euo pipefail" in att[0].get_content()
    assert "--limits=memory=128Mi" in pipeline.fix_script(report.incident_id)
