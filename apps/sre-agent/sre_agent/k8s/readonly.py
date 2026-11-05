"""The only way the agent talks to Kubernetes: an explicit allowlist of read operations.

There is deliberately no generic "call any API" method, nothing that reads
Secrets or ConfigMap contents, and no exec/attach/port-forward. Every
operation is recorded as its kubectl equivalent for the incident report.
The cluster's RBAC (deploy/rbac.yaml) enforces the same thing server-side.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Protocol

from ..config import ClusterConfig, Limits
from .. import metrics

log = logging.getLogger(__name__)

# operation name -> kubectl equivalent (for the report); the allowlist itself
ALLOWED_OPERATIONS: dict[str, str] = {
    "read_pod": "kubectl get pod {name} -n {namespace} -o yaml",
    "list_pods": "kubectl get pods -n {namespace} -o wide{selector}",
    "list_pods_on_node": "kubectl get pods -A -o wide --field-selector spec.nodeName={name}",
    "read_pod_log": "kubectl logs {name} -n {namespace} -c {container}{previous} --tail={tail} --timestamps",
    "list_events": "kubectl get events -n {namespace} --sort-by=.metadata.creationTimestamp{selector}",
    "read_deployment": "kubectl get deployment {name} -n {namespace} -o yaml",
    "list_replicasets": "kubectl get replicasets -n {namespace}{selector}",
    "read_replicaset": "kubectl get replicaset {name} -n {namespace} -o yaml",
    "read_node": "kubectl describe node {name}",
    "list_nodes": "kubectl get nodes -o wide",
    "pod_metrics": "kubectl top pods -n {namespace}",
    "node_metrics": "kubectl top nodes",
    "list_services": "kubectl get services -n {namespace}",
    "read_endpoints": "kubectl get endpoints {name} -n {namespace}",
    "read_pvc": "kubectl describe pvc {name} -n {namespace}",
    "list_pvcs": "kubectl get pvc -n {namespace}",
    "list_storage_classes": "kubectl get storageclass",
    "list_resource_quotas": "kubectl describe resourcequota -n {namespace}",
    "list_network_policies": "kubectl get networkpolicy -n {namespace}",
}


class KubeReader(Protocol):
    """What the diagnostic engine needs. Implemented by ReadOnlyKube and test fakes."""

    cluster: str
    commands: list[str]

    def call(self, operation: str, **kwargs: Any) -> Any: ...


class OperationNotAllowed(PermissionError):
    pass


class KubeApiError(RuntimeError):
    def __init__(self, operation: str, status: int | None, reason: str):
        super().__init__(f"{operation}: {status or ''} {reason}".strip())
        self.status = status


class ReadOnlyKube:
    """Read-only access to one cluster, with that cluster's own credentials."""

    def __init__(self, cluster: ClusterConfig, limits: Limits):
        from kubernetes import client, config  # imported here: tests can run without a cluster

        self.cluster = cluster.name
        self.limits = limits
        self.commands: list[str] = []
        cfg = client.Configuration()
        if cluster.in_cluster:
            config.load_incluster_config(client_configuration=cfg)
        else:
            # A dedicated Configuration per cluster: credentials are never shared
            # through the client library's global default.
            config.load_kube_config(config_file=cluster.kubeconfig, context=cluster.context,
                                    client_configuration=cfg)
        api = client.ApiClient(cfg)
        self._api = api
        self._core = client.CoreV1Api(api)
        self._apps = client.AppsV1Api(api)
        self._storage = client.StorageV1Api(api)
        self._net = client.NetworkingV1Api(api)
        self._custom = client.CustomObjectsApi(api)
        self._ops: dict[str, Callable[..., Any]] = {
            "read_pod": lambda name, namespace: self._core.read_namespaced_pod(name, namespace, **self._t()),
            "list_pods": lambda namespace, selector="": self._core.list_namespaced_pod(
                namespace, label_selector=selector, limit=200, **self._t()),
            "list_pods_on_node": lambda name: self._core.list_pod_for_all_namespaces(
                field_selector=f"spec.nodeName={name}", limit=250, **self._t()),
            "read_pod_log": self._read_log,
            "list_events": lambda namespace, selector="": self._core.list_namespaced_event(
                namespace, field_selector=selector or None, limit=500, **self._t()),
            "read_deployment": lambda name, namespace: self._apps.read_namespaced_deployment(name, namespace, **self._t()),
            "list_replicasets": lambda namespace, selector="": self._apps.list_namespaced_replica_set(
                namespace, label_selector=selector, limit=50, **self._t()),
            "read_replicaset": lambda name, namespace: self._apps.read_namespaced_replica_set(name, namespace, **self._t()),
            "read_node": lambda name: self._core.read_node(name, **self._t()),
            "list_nodes": lambda: self._core.list_node(limit=500, **self._t()),
            "pod_metrics": lambda namespace: self._custom.list_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1", namespace, "pods", **self._t()),
            "node_metrics": lambda: self._custom.list_cluster_custom_object(
                "metrics.k8s.io", "v1beta1", "nodes", **self._t()),
            "list_services": lambda namespace: self._core.list_namespaced_service(namespace, limit=200, **self._t()),
            "read_endpoints": lambda name, namespace: self._core.read_namespaced_endpoints(name, namespace, **self._t()),
            "read_pvc": lambda name, namespace: self._core.read_namespaced_persistent_volume_claim(
                name, namespace, **self._t()),
            "list_pvcs": lambda namespace: self._core.list_namespaced_persistent_volume_claim(
                namespace, limit=100, **self._t()),
            "list_storage_classes": lambda: self._storage.list_storage_class(**self._t()),
            "list_resource_quotas": lambda namespace: self._core.list_namespaced_resource_quota(namespace, **self._t()),
            "list_network_policies": lambda namespace: self._net.list_namespaced_network_policy(
                namespace, limit=100, **self._t()),
        }
        assert set(self._ops) == set(ALLOWED_OPERATIONS), "allowlist and implementations must match"

    def _t(self) -> dict[str, Any]:
        return {"_request_timeout": self.limits.api_timeout_seconds}

    def _read_log(self, name: str, namespace: str, container: str, previous: bool = False, tail: int = 200) -> str:
        return self._core.read_namespaced_pod_log(
            name, namespace, container=container, previous=previous, tail_lines=tail,
            timestamps=True, limit_bytes=self.limits.log_max_bytes, **self._t())

    def call(self, operation: str, **kwargs: Any) -> Any:
        if operation not in ALLOWED_OPERATIONS:
            raise OperationNotAllowed(f"'{operation}' is not on the read-only allowlist")
        self.commands.append(describe_command(operation, kwargs))
        from kubernetes.client.exceptions import ApiException

        try:
            result = self._ops[operation](**kwargs)
        except ApiException as e:  # 403 from RBAC lands here too
            metrics.K8S_API_ERRORS.labels(self.cluster, operation, str(e.status)).inc()
            raise KubeApiError(operation, e.status, e.reason or "") from None
        except Exception as e:  # timeouts, connection errors
            metrics.K8S_API_ERRORS.labels(self.cluster, operation, type(e).__name__).inc()
            raise KubeApiError(operation, None, type(e).__name__) from None
        if isinstance(result, (str, dict)):
            return result
        return self._api.sanitize_for_serialization(result)


def describe_command(operation: str, kwargs: dict[str, Any]) -> str:
    template = ALLOWED_OPERATIONS[operation]
    values = {"name": "", "namespace": "", "container": "", "tail": kwargs.get("tail", 200), **kwargs}
    values["previous"] = " --previous" if kwargs.get("previous") else ""
    sel = kwargs.get("selector") or ""
    values["selector"] = (f" --field-selector {sel}" if operation == "list_events" else f" -l {sel}") if sel else ""
    try:
        return template.format(**values)
    except (KeyError, IndexError):
        return operation
