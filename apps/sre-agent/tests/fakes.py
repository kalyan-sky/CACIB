"""A fake cluster: canned Kubernetes API objects (the same JSON shapes the API returns)."""

from __future__ import annotations

import copy
from typing import Any

from sre_agent.k8s.readonly import ALLOWED_OPERATIONS, KubeApiError, OperationNotAllowed, describe_command

NS = "payments"


def deployment(name: str, ns: str = NS, image: str = "registry.example/app:1.0", replicas: int = 2,
               available: int = 0, mem_limit: str = "128Mi", extra_conditions: list | None = None) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": ns,
                     "labels": {"app.kubernetes.io/name": name, "bank.sim/tier": "tier-1",
                                "bank.sim/owner-team": "team-payments", "bank.sim/exposure": "edge"},
                     "annotations": {"deployment.kubernetes.io/revision": "3",
                                     "argocd.argoproj.io/tracking-id": f"bank-sim:apps/Deployment:{ns}/{name}",
                                     "kubectl.kubernetes.io/last-applied-configuration": '{"env":[{"name":"DB_PASSWORD","value":"hunter2"}]}'}},
        "spec": {"replicas": replicas, "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
                 "strategy": {"type": "RollingUpdate"},
                 "template": {"spec": {"containers": [{"name": "app", "image": image,
                                                       "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                                                                     "limits": {"memory": mem_limit}}}]}}},
        "status": {"replicas": replicas, "availableReplicas": available, "readyReplicas": available,
                   "unavailableReplicas": replicas - available,
                   "conditions": extra_conditions or [{"type": "Available", "status": "False",
                                                       "reason": "MinimumReplicasUnavailable"}]},
    }


def pod(name: str, dep: str, ns: str = NS, *, phase: str = "Running", state: dict | None = None,
        last_state: dict | None = None, ready: bool = False, restarts: int = 0, node: str | None = "ip-10-20-1-10",
        image: str = "registry.example/app:1.0", mem_limit: str = "128Mi", cpu_request: str = "50m",
        env: list | None = None, volumes: list | None = None, node_selector: dict | None = None,
        liveness: dict | None = None, readiness: dict | None = None) -> dict[str, Any]:
    container_status = {"name": "app", "image": image, "ready": ready, "restartCount": restarts,
                        "state": state or {"running": {"startedAt": "2026-11-05T10:00:00Z"}},
                        "lastState": last_state or {}}
    return {
        "metadata": {"name": name, "namespace": ns, "labels": {"app.kubernetes.io/name": dep},
                     "creationTimestamp": "2026-11-05T09:58:00Z",
                     "ownerReferences": [{"kind": "ReplicaSet", "name": f"{dep}-7d9f8c6b5d"}]},
        "spec": {"nodeName": node, "nodeSelector": node_selector or {},
                 "containers": [{"name": "app", "image": image,
                                 "env": env or [{"name": "DB_PASSWORD", "value": "s3cr3t-value"},
                                                {"name": "LOG_LEVEL", "value": "info"}],
                                 "envFrom": [{"configMapRef": {"name": f"{dep}-config"}}],
                                 "resources": {"requests": {"cpu": cpu_request, "memory": "64Mi"},
                                               "limits": {"memory": mem_limit}},
                                 "readinessProbe": readiness or {"httpGet": {"path": "/readyz", "port": "http"}, "periodSeconds": 5},
                                 "livenessProbe": liveness or {"httpGet": {"path": "/healthz", "port": "http"},
                                                               "initialDelaySeconds": 5, "periodSeconds": 10}}],
                 "volumes": volumes or []},
        "status": {"phase": phase, "containerStatuses": [container_status] if phase != "Pending" or state else [],
                   "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


def event(kind: str, name: str, reason: str, message: str, etype: str = "Warning", ns: str = NS, count: int = 1,
          ts: str = "2026-11-05T10:01:00Z", component: str = "kubelet") -> dict[str, Any]:
    return {"metadata": {"namespace": ns, "creationTimestamp": ts}, "type": etype, "reason": reason, "message": message,
            "count": count, "lastTimestamp": ts, "source": {"component": component},
            "involvedObject": {"kind": kind, "name": name, "namespace": ns}}


def node(name: str, ready: str = "True", reason: str = "KubeletReady", message: str = "kubelet is posting ready status",
         pressure: dict | None = None, taints: list | None = None, cpu: str = "1930m", labels: dict | None = None) -> dict[str, Any]:
    conds = [{"type": "Ready", "status": ready, "reason": reason, "message": message,
              "lastHeartbeatTime": "2026-11-05T09:50:00Z", "lastTransitionTime": "2026-11-05T09:55:00Z"}]
    for t in ("MemoryPressure", "DiskPressure", "PIDPressure"):
        conds.append({"type": t, "status": (pressure or {}).get(t, "False"), "reason": f"Kubelet{t}",
                      "message": f"kubelet has {t}" if (pressure or {}).get(t) == "True" else f"no {t}"})
    return {"metadata": {"name": name, "labels": {"node.kubernetes.io/instance-type": "m7i-flex.large",
                                                  "topology.kubernetes.io/zone": "ap-southeast-2a", **(labels or {})}},
            "spec": {"providerID": f"aws:///ap-southeast-2a/i-0abc{name[-2:]}", "taints": taints or []},
            "status": {"conditions": conds, "allocatable": {"cpu": cpu, "memory": "7364Mi", "pods": "110"},
                       "capacity": {"cpu": "2", "memory": "7990Mi"}, "nodeInfo": {"kubeletVersion": "v1.37.1"}}}


class FakeKube:
    """Implements KubeReader. Answers only allowlisted operations, from fixtures."""

    def __init__(self, cluster: str = "test-eks", *, pods: list | None = None, deployments: list | None = None,
                 events: list | None = None, nodes: list | None = None, logs: dict | None = None,
                 pvcs: list | None = None, storage_classes: list | None = None, services: list | None = None,
                 endpoints: dict | None = None, quotas: list | None = None, pod_metrics: list | None = None,
                 forbidden: set[str] | None = None):
        self.cluster = cluster
        self.commands: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.pods = pods or []
        self.deployments = deployments or []
        self.events = events or []
        self.nodes = nodes or []
        self.logs = logs or {}
        self.pvcs = pvcs or []
        self.storage_classes = storage_classes or []
        self.services = services or []
        self.endpoints = endpoints or {}
        self.quotas = quotas or []
        self.pod_metrics = pod_metrics or []
        self.forbidden = forbidden or set()

    def call(self, operation: str, **kw: Any) -> Any:
        if operation not in ALLOWED_OPERATIONS:
            raise OperationNotAllowed(operation)
        self.calls.append((operation, kw))
        self.commands.append(describe_command(operation, kw))
        if operation in self.forbidden:
            raise KubeApiError(operation, 403, "Forbidden")
        ns = kw.get("namespace")
        name = kw.get("name")
        if operation == "read_pod":
            return self._one(self.pods, name, ns, operation)
        if operation == "list_pods":
            sel = dict(p.split("=") for p in kw.get("selector", "").split(",") if p)
            return {"items": [copy.deepcopy(p) for p in self.pods if p["metadata"]["namespace"] == ns
                              and all(p["metadata"]["labels"].get(k) == v for k, v in sel.items())]}
        if operation == "list_pods_on_node":
            return {"items": [p for p in self.pods if p["spec"].get("nodeName") == name]}
        if operation == "read_pod_log":
            key = f"{name}/{kw['container']}" + ("/previous" if kw.get("previous") else "")
            if key not in self.logs:
                raise KubeApiError(operation, 400, "previous terminated container not found" if kw.get("previous") else "no logs")
            return self.logs[key]
        if operation == "list_events":
            sel = dict(p.split("=", 1) for p in (kw.get("selector") or "").split(",") if p)
            items = [e for e in self.events if e["metadata"]["namespace"] == ns]
            if sel:
                items = [e for e in items if e["involvedObject"]["kind"] == sel.get("involvedObject.kind")
                         and e["involvedObject"]["name"] == sel.get("involvedObject.name")]
            return {"items": items}
        if operation == "read_deployment":
            return self._one(self.deployments, name, ns, operation)
        if operation == "list_replicasets":
            return {"items": [{"metadata": {"name": f"{d['metadata']['name']}-7d9f8c6b5d",
                                            "annotations": {"deployment.kubernetes.io/revision": "3"}},
                               "spec": {"replicas": d["spec"]["replicas"], "template": d["spec"]["template"]},
                               "status": {"readyReplicas": 0}} for d in self.deployments if d["metadata"]["namespace"] == ns]}
        if operation == "read_replicaset":
            dep = name.rsplit("-", 1)[0]
            return {"metadata": {"name": name, "ownerReferences": [{"kind": "Deployment", "name": dep}]}}
        if operation == "read_node":
            return self._one(self.nodes, name, None, operation)
        if operation == "list_nodes":
            return {"items": self.nodes}
        if operation == "pod_metrics":
            return {"items": self.pod_metrics}
        if operation == "node_metrics":
            return {"items": [{"metadata": {"name": n["metadata"]["name"]}, "usage": {"cpu": "250m", "memory": "2100Mi"}}
                              for n in self.nodes]}
        if operation == "list_services":
            return {"items": self.services}
        if operation == "read_endpoints":
            return self.endpoints.get(name, {"subsets": []})
        if operation == "read_pvc":
            return self._one(self.pvcs, name, ns, operation)
        if operation == "list_pvcs":
            return {"items": self.pvcs}
        if operation == "list_storage_classes":
            return {"items": self.storage_classes}
        if operation == "list_resource_quotas":
            return {"items": self.quotas}
        if operation == "list_network_policies":
            return {"items": []}
        raise AssertionError(f"fake does not implement {operation}")

    def _one(self, items: list, name: str, ns: str | None, op: str) -> dict[str, Any]:
        for i in items:
            if i["metadata"]["name"] == name and (ns is None or i["metadata"].get("namespace") == ns):
                return copy.deepcopy(i)
        raise KubeApiError(op, 404, "NotFound")


def am_alert(alertname: str, *, status: str = "firing", severity: str = "critical", fingerprint: str = "fp1",
             **labels: str) -> dict[str, Any]:
    return {"status": status, "labels": {"alertname": alertname, "severity": severity, **labels},
            "annotations": {"summary": f"{alertname} fired"}, "startsAt": "2026-11-05T10:00:00Z",
            "fingerprint": fingerprint}


def webhook(*alerts: dict[str, Any]) -> dict[str, Any]:
    return {"version": "4", "status": "firing", "receiver": "sre-agent", "alerts": list(alerts)}
