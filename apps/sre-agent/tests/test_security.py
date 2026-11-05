"""Test 7: the agent cannot change a cluster, read Secrets, exec into containers, or act on approval."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml
from fakes import NS, FakeKube, am_alert, deployment, pod, webhook
from fastapi.testclient import TestClient

from sre_agent.api import create_app
from sre_agent.k8s.readonly import ALLOWED_OPERATIONS, OperationNotAllowed
from sre_agent.redact import redact, redact_text

PKG = Path(__file__).resolve().parents[1] / "sre_agent"
DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection", "escalate", "bind", "impersonate", "approve", "sign", "*"}


def test_allowlist_has_only_read_operations():
    for op in ALLOWED_OPERATIONS:
        assert op.split("_")[0] in ("read", "list", "pod", "node"), op
    assert not any("secret" in op or "exec" in op or "attach" in op or "portforward" in op or "proxy" in op
                   for op in ALLOWED_OPERATIONS)
    assert not any(re.search(r"\b(delete|apply|patch|edit|scale|exec|cp|create|replace|rollout)\b", cmd)
                   for cmd in ALLOWED_OPERATIONS.values())


def test_operations_outside_the_allowlist_are_refused():
    fake = FakeKube()
    for op in ("delete_namespaced_pod", "patch_namespaced_deployment", "read_namespaced_secret",
               "connect_get_namespaced_pod_exec", "create_namespaced_pod", "list_secrets"):
        with pytest.raises(OperationNotAllowed):
            fake.call(op, name="x", namespace=NS)


def test_source_never_calls_kubernetes_write_or_secret_or_exec_apis():
    """Static check over the whole package: no client method that writes, reads Secrets or execs."""
    forbidden = re.compile(r"^(create|patch|replace|delete|connect)_|secret|_exec$|stream$")
    offenders = []
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) \
                    and node.value.attr in ("_core", "_apps", "_storage", "_net", "_custom", "_api"):
                if forbidden.search(node.attr):
                    offenders.append(f"{path.name}: {node.value.attr}.{node.attr}")
            if isinstance(node, ast.ImportFrom) and node.module and "kubernetes.stream" in node.module:
                offenders.append(f"{path.name}: imports kubernetes.stream (exec)")
    assert offenders == []


def test_rbac_manifest_is_read_only_without_secrets_or_exec():
    docs = [d for d in yaml.safe_load_all((DEPLOY / "rbac.yaml").read_text()) if d]
    roles = [d for d in docs if d["kind"] in ("ClusterRole", "Role")]
    assert roles and not any(d["kind"] == "ClusterRoleBinding" and d["roleRef"]["name"] == "cluster-admin" for d in docs)
    for role in roles:
        assert "aggregationRule" not in role
        for rule in role["rules"]:
            assert set(rule["verbs"]) <= {"get", "list", "watch"}, rule
            assert not WRITE_VERBS & set(rule["verbs"])
            resources = set(rule.get("resources", []))
            assert "*" not in resources and "*" not in rule.get("apiGroups", [])
            assert not resources & {"secrets", "configmaps", "pods/exec", "pods/attach", "pods/portforward",
                                    "nodes/proxy", "services/proxy", "serviceaccounts/token", "pods/eviction"}, rule


def test_approval_does_not_touch_the_cluster(settings, store, smtp):
    fake = FakeKube(pods=[pod("app-7d9f8c6b5d-x7k2p", "app", state={"waiting": {"reason": "CrashLoopBackOff"}},
                              last_state={"terminated": {"reason": "OOMKilled", "exitCode": 137}}, restarts=4)],
                    deployments=[deployment("app")])
    app = create_app(settings, kube_factory=lambda name: fake, store=store, start_workers=False)
    with TestClient(app) as client:
        r = client.post("/api/v1/alerts?cluster=test-eks", json=webhook(am_alert("PodOOMKilled", namespace=NS,
                                                                                 pod="app-7d9f8c6b5d-x7k2p")),
                        headers={"Authorization": "Bearer webhook-token-0123456789"})
        incident_id = r.json()["created"][0]
        app.state.pipeline.process(incident_id, app.state.pipeline.queue.get_nowait()[1])
        calls_before = list(fake.calls)
        approval = store.approvals(incident_id=incident_id)[0]
        r = client.post(f"/api/v1/approvals/{approval['id']}/approve", json={"comment": "go"},
                        auth=("alice", "alice-password-123"))
        assert r.status_code == 200 and r.json()["status"] == "Approved"
        assert "does not execute" in r.json()["note"]
        assert fake.calls == calls_before  # not a single API call, read or write, after approval
        assert all(op in ALLOWED_OPERATIONS for op, _ in fake.calls)


def test_secret_values_are_redacted():
    obj = {"env": [{"name": "DB_PASSWORD", "value": "p4ss"}, {"name": "API_TOKEN", "value": "t0k"},
                   {"name": "LOG_LEVEL", "value": "info"}],
           "metadata": {"annotations": {"kubectl.kubernetes.io/last-applied-configuration": '{"p":"x"}'}},
           "secretKeyRef": {"name": "db", "key": "password"}}
    out = redact(obj)
    assert out["env"][0]["value"] == "[REDACTED]" and out["env"][1]["value"] == "[REDACTED]"
    assert out["env"][2]["value"] == "info"
    assert out["metadata"]["annotations"]["kubectl.kubernetes.io/last-applied-configuration"] == "[REDACTED]"
    assert out["secretKeyRef"] == {"name": "db", "key": "password"}  # the reference is fine, it is not a value
    text = ("postgres://user:pa55@db:5432 AKIAABCDEFGHIJKLMNOP password=abc token: xyz "
            "glpat-abcdefghijklmnopqrstuv -----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----")
    red = redact_text(text)
    for leaked in ("pa55", "AKIAABCDEFGHIJKLMNOP", "=abc", ": xyz", "glpat-abcdefghijklmnopqrstuv", "MIIE"):
        assert leaked not in red, leaked
