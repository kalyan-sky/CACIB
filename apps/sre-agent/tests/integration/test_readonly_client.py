"""Integration: the real ReadOnlyKube + official Kubernetes client against a stub API server.

Proves on the wire that a whole investigation only sends GET requests, uses the
kubeconfig of the cluster it was given, and survives RBAC 403s.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from sre_agent.config import ClusterConfig, Limits
from sre_agent.diagnostics import DiagnosticEngine
from sre_agent.k8s.readonly import ReadOnlyKube
from sre_agent.models import AmAlert, NormalizedAlert

POD = "web-7d9f8c6b5d-x7k2p"


def k8s_objects() -> dict[str, object]:
    pod = {"apiVersion": "v1", "kind": "Pod",
           "metadata": {"name": POD, "namespace": "shop", "labels": {"app": "web"},
                        "ownerReferences": [{"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": "web-7d9f8c6b5d",
                                             "uid": "1", "controller": True}]},
           "spec": {"nodeName": "node-a", "containers": [{"name": "app", "image": "nginx:1.27",
                                                          "resources": {"limits": {"memory": "64Mi"}}}]},
           "status": {"phase": "Running", "containerStatuses": [
               {"name": "app", "image": "nginx:1.27", "imageID": "", "ready": False, "restartCount": 6,
                "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]}}
    rs = {"apiVersion": "apps/v1", "kind": "ReplicaSet",
          "metadata": {"name": "web-7d9f8c6b5d", "namespace": "shop",
                       "ownerReferences": [{"apiVersion": "apps/v1", "kind": "Deployment", "name": "web", "uid": "2"}]},
          "spec": {"selector": {"matchLabels": {"app": "web"}}}}
    dep = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": "shop"},
           "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "web"}},
                    "template": {"metadata": {"labels": {"app": "web"}},
                                 "spec": {"containers": [{"name": "app", "image": "nginx:1.27"}]}}},
           "status": {"replicas": 1, "unavailableReplicas": 1}}
    node = {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "node-a"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}], "allocatable": {"cpu": "2"}}}
    lst = lambda kind, items: {"apiVersion": "v1", "kind": f"{kind}List", "metadata": {}, "items": items}  # noqa: E731
    return {
        f"/api/v1/namespaces/shop/pods/{POD}": pod,
        "/apis/apps/v1/namespaces/shop/replicasets/web-7d9f8c6b5d": rs,
        "/apis/apps/v1/namespaces/shop/deployments/web": dep,
        "/apis/apps/v1/namespaces/shop/replicasets": lst("ReplicaSet", [rs]),
        "/api/v1/namespaces/shop/pods": lst("Pod", [pod]),
        "/api/v1/namespaces/shop/events": lst("Event", []),
        "/api/v1/namespaces/default/events": lst("Event", []),
        "/api/v1/nodes/node-a": node,
        "/api/v1/namespaces/shop/services": lst("Service", []),
        "/api/v1/namespaces/shop/resourcequotas": lst("ResourceQuota", []),
        f"/api/v1/namespaces/shop/pods/{POD}/log": "boot\nloading cache\n",
    }


class StubApi(BaseHTTPRequestHandler):
    objects: dict[str, object] = {}
    seen: list[tuple[str, str, str]] = []
    forbidden_prefixes = ("/apis/metrics.k8s.io",)

    def log_message(self, *a: object) -> None:
        pass

    def _handle(self) -> None:
        path = self.path.split("?")[0]
        StubApi.seen.append((self.command, path, self.headers.get("Authorization", "")))
        if self.command != "GET":
            self._send(405, {"kind": "Status", "code": 405, "reason": "MethodNotAllowed"})
        elif path.startswith(self.forbidden_prefixes):
            self._send(403, {"kind": "Status", "apiVersion": "v1", "status": "Failure", "reason": "Forbidden", "code": 403})
        elif path in self.objects:
            body = self.objects[path]
            self._send(200, body)
        else:
            self._send(404, {"kind": "Status", "apiVersion": "v1", "status": "Failure", "reason": "NotFound", "code": 404})

    def _send(self, code: int, body: object) -> None:
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain" if isinstance(body, str) else "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


@pytest.fixture
def api_server(tmp_path: Path):
    StubApi.objects, StubApi.seen = k8s_objects(), []
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{server.server_address[1]}"

    def kubeconfig(name: str, token: str) -> str:
        path = tmp_path / f"{name}.yaml"
        path.write_text(yaml.safe_dump({
            "apiVersion": "v1", "kind": "Config", "current-context": name,
            "clusters": [{"name": name, "cluster": {"server": host}}],
            "users": [{"name": name, "user": {"token": token}}],
            "contexts": [{"name": name, "context": {"cluster": name, "user": name}}]}))
        return str(path)

    yield kubeconfig
    server.shutdown()


def test_investigation_sends_only_get_requests(api_server):
    cfg = ClusterConfig(name="uat-eks", environment="uat", kubeconfig=api_server("uat-eks", "uat-readonly-token"))
    kube = ReadOnlyKube(cfg, Limits(api_timeout_seconds=5))
    alert = NormalizedAlert.from_alert(
        AmAlert(status="firing", labels={"alertname": "KubePodCrashLooping", "namespace": "shop", "pod": POD,
                                         "severity": "critical"}), "uat-eks", "uat", [])
    ev = DiagnosticEngine(kube, Limits()).investigate(alert)

    methods = {m for m, _, _ in StubApi.seen}
    assert methods == {"GET"}, StubApi.seen
    assert all(auth == "Bearer uat-readonly-token" for _, _, auth in StubApi.seen)  # this cluster's credentials
    paths = {p for _, p, _ in StubApi.seen}
    assert f"/api/v1/namespaces/shop/pods/{POD}/log" in paths
    assert not any("secrets" in p or "/exec" in p or "configmaps" in p for p in paths)
    # the evidence came through the real client
    assert ev.deployment and ev.deployment["name"] == "web"
    assert ev.pods[0]["containers"][0]["last_terminated"]["reason"] == "OOMKilled"
    assert any("403" in e for e in ev.errors)  # metrics forbidden -> recorded, not fatal
    assert any(c.startswith("kubectl logs") and "--previous" in c for c in ev.commands_executed)


def test_two_clusters_use_their_own_credentials(api_server):
    a = ReadOnlyKube(ClusterConfig(name="a", kubeconfig=api_server("a", "token-a")), Limits())
    b = ReadOnlyKube(ClusterConfig(name="b", kubeconfig=api_server("b", "token-b")), Limits())
    a.call("read_node", name="node-a")
    b.call("read_node", name="node-a")
    a.call("read_node", name="node-a")
    assert [auth for _, _, auth in StubApi.seen] == ["Bearer token-a", "Bearer token-b", "Bearer token-a"]
