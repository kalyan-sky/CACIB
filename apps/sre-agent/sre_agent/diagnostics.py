"""Diagnostic engine: collects evidence for one alert using read-only API calls only."""

from __future__ import annotations

import logging
import time
from typing import Any

from .config import Limits
from .k8s.readonly import KubeApiError, KubeReader
from .models import InvestigationEvidence, NormalizedAlert
from .redact import redact, redact_text

log = logging.getLogger(__name__)


class Deadline:
    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds

    @property
    def expired(self) -> bool:
        return time.monotonic() > self.end


class DiagnosticEngine:
    def __init__(self, kube: KubeReader, limits: Limits):
        self.kube = kube
        self.limits = limits

    # ------------------------------------------------------------------ helpers

    def _get(self, ev: InvestigationEvidence, op: str, **kw: Any) -> Any:
        try:
            return self.kube.call(op, **kw)
        except KubeApiError as e:
            ev.errors.append(str(e))
            return None

    def _events_for(self, ev: InvestigationEvidence, namespace: str, kind: str, name: str) -> list[dict[str, Any]]:
        sel = f"involvedObject.kind={kind},involvedObject.name={name}"
        res = self._get(ev, "list_events", namespace=namespace, selector=sel)
        return [summarize_event(e) for e in (res or {}).get("items", [])]

    # ------------------------------------------------------------------ main entry

    def investigate(self, alert: NormalizedAlert) -> InvestigationEvidence:
        start = time.monotonic()
        deadline = Deadline(self.limits.investigation_timeout_seconds)
        ev = InvestigationEvidence(cluster=alert.cluster, namespace=alert.namespace)
        ns = alert.namespace

        try:
            pods: list[dict[str, Any]] = []
            if ns and alert.pod:
                pod = self._get(ev, "read_pod", name=alert.pod, namespace=ns)
                if pod:
                    pods.append(pod)
            deployment = None
            if ns:
                dep_name = alert.deployment or (owner_deployment(self, ev, pods[0], ns) if pods else None)
                if not dep_name and alert.workload and not alert.pod:
                    dep_name = alert.workload
                if dep_name:
                    deployment = self._get(ev, "read_deployment", name=dep_name, namespace=ns)
                if deployment and len(pods) < self.limits.max_pods:
                    pods += self._unhealthy_pods_of(ev, deployment, ns, exclude={p["metadata"]["name"] for p in pods})

            if deployment:
                ev.deployment = summarize_deployment(deployment)
                ev.targets["deployment"] = deployment["metadata"]["name"]
                sel = selector_string(deployment)
                rs = self._get(ev, "list_replicasets", namespace=ns, selector=sel) if sel else None
                ev.replicasets = [summarize_replicaset(r) for r in (rs or {}).get("items", [])][:10]
                ev.events += self._events_for(ev, ns, "Deployment", deployment["metadata"]["name"])
                for r in ev.replicasets[:3]:  # FailedCreate (quota, admission) lands on the ReplicaSet
                    ev.events += self._events_for(ev, ns, "ReplicaSet", r["name"])

            node_names: set[str] = set()
            for pod in pods[: self.limits.max_pods]:
                if deadline.expired:
                    ev.truncated = True
                    break
                summary = summarize_pod(pod)
                ev.pods.append(summary)
                ev.events += self._events_for(ev, ns, "Pod", summary["name"])
                self._collect_logs(ev, summary, alert.container)
                if summary.get("node"):
                    node_names.add(summary["node"])
            if pods:
                ev.targets["pods"] = [p["name"] for p in ev.pods]

            if any(p["phase"] == "Pending" and not p.get("node") for p in ev.pods):
                # scheduling problem: compare the pod with what every node offers
                all_nodes = self._get(ev, "list_nodes")
                ev.nodes += [summarize_node(n) for n in (all_nodes or {}).get("items", [])[:10]]
            if alert.node:
                node_names.add(alert.node)
                ev.targets["node"] = alert.node
            for name in sorted(node_names - {n["name"] for n in ev.nodes})[:3]:
                node = self._get(ev, "read_node", name=name)
                if node:
                    ev.nodes.append(summarize_node(node))
                    ev.events += self._events_for(ev, "default", "Node", name)
            if alert.node and not ev.nodes:
                ev.errors.append(f"node {alert.node} not found")
            if alert.node:
                on_node = self._get(ev, "list_pods_on_node", name=alert.node)
                if on_node:
                    ev.targets["pods_on_node"] = len(on_node.get("items", []))

            if ns and (alert.pvc or any(p.get("pvc_claims") for p in ev.pods)):
                claims = {alert.pvc} if alert.pvc else set()
                for p in ev.pods:
                    claims.update(p.get("pvc_claims", []))
                for claim in sorted(c for c in claims if c)[:3]:
                    pvc = self._get(ev, "read_pvc", name=claim, namespace=ns)
                    if pvc:
                        ev.pvcs.append(summarize_pvc(pvc))
                        ev.events += self._events_for(ev, ns, "PersistentVolumeClaim", claim)
                scs = self._get(ev, "list_storage_classes")
                ev.storage_classes = [s["metadata"]["name"] + (" (default)" if is_default_sc(s) else "")
                                      for s in (scs or {}).get("items", [])]

            if ns and ev.pods and not deadline.expired:
                self._collect_services(ev, ns, pods[0])
            if ns and not deadline.expired:
                quotas = self._get(ev, "list_resource_quotas", namespace=ns)
                ev.quotas = [summarize_quota(q) for q in (quotas or {}).get("items", [])]
                if not ev.events:  # nothing object-specific: fall back to the namespace's recent events
                    res = self._get(ev, "list_events", namespace=ns)
                    ev.events = [summarize_event(e) for e in (res or {}).get("items", [])]

            if is_dns_alert(alert) and not deadline.expired:
                cd = self._get(ev, "read_deployment", name="coredns", namespace="kube-system")
                if cd:
                    ev.coredns = summarize_deployment(cd)
                    ev.events += self._events_for(ev, "kube-system", "Deployment", "coredns")

            if not deadline.expired:
                self._collect_metrics(ev, ns, node_names)
        except Exception as e:  # never lose the incident because one collector failed
            log.exception("investigation failed")
            ev.errors.append(f"collector error: {type(e).__name__}: {e}")

        if deadline.expired:
            ev.truncated = True
        ev.events = dedupe_events(ev.events)[-self.limits.max_events:]
        ev.commands_executed = list(dict.fromkeys(self.kube.commands))
        ev.duration_seconds = round(time.monotonic() - start, 2)
        return InvestigationEvidence.model_validate(redact(ev.model_dump()))

    # ------------------------------------------------------------------ collectors

    def _unhealthy_pods_of(self, ev: InvestigationEvidence, deployment: dict[str, Any], ns: str,
                           exclude: set[str]) -> list[dict[str, Any]]:
        sel = selector_string(deployment)
        if not sel:
            return []
        res = self._get(ev, "list_pods", namespace=ns, selector=sel)
        items = [p for p in (res or {}).get("items", []) if p["metadata"]["name"] not in exclude]
        # unhealthy first, newest first
        items.sort(key=lambda p: (pod_is_healthy(p), p["metadata"].get("creationTimestamp", "")), reverse=False)
        unhealthy = [p for p in items if not pod_is_healthy(p)]
        chosen = unhealthy or items[:1]
        return chosen[: max(0, self.limits.max_pods - len(exclude))]

    def _collect_logs(self, ev: InvestigationEvidence, pod: dict[str, Any], only_container: str | None) -> None:
        for c in pod.get("containers", []):
            if only_container and c["name"] != only_container and len(pod["containers"]) > 1:
                continue
            never_started = (c.get("state", "").startswith("unknown") or c.get("image_pull_failed")
                             or c.get("state") in ("waiting:ContainerCreating", "waiting:PodInitializing",
                                                   "waiting:CreateContainerConfigError"))
            if never_started and not c.get("restart_count"):
                continue  # no container has run yet: there are no logs to read
            key = f"{pod['name']}/{c['name']}"
            text = self._get(ev, "read_pod_log", name=pod["name"], namespace=ev.namespace, container=c["name"],
                             tail=self.limits.log_tail_lines)
            if text:
                ev.logs[key] = clip(text, self.limits.log_max_bytes)
            if c.get("restart_count", 0) > 0 or c.get("last_terminated"):
                prev = self._get(ev, "read_pod_log", name=pod["name"], namespace=ev.namespace,
                                 container=c["name"], previous=True, tail=self.limits.log_tail_lines)
                if prev:
                    ev.logs[f"{key}/previous"] = clip(prev, self.limits.log_max_bytes)

    def _collect_services(self, ev: InvestigationEvidence, ns: str, pod: dict[str, Any]) -> None:
        labels = pod["metadata"].get("labels", {}) or {}
        svcs = self._get(ev, "list_services", namespace=ns)
        for svc in (svcs or {}).get("items", []):
            sel = (svc.get("spec") or {}).get("selector") or {}
            if not sel or any(labels.get(k) != v for k, v in sel.items()):
                continue
            name = svc["metadata"]["name"]
            eps = self._get(ev, "read_endpoints", name=name, namespace=ns) or {}
            ready = sum(len(s.get("addresses") or []) for s in eps.get("subsets") or [])
            not_ready = sum(len(s.get("notReadyAddresses") or []) for s in eps.get("subsets") or [])
            ev.services.append({
                "name": name, "selector": sel,
                "ports": [{"port": p.get("port"), "targetPort": p.get("targetPort")} for p in svc["spec"].get("ports", [])],
                "ready_endpoints": ready, "not_ready_endpoints": not_ready,
            })
            if len(ev.services) >= 3:
                break

    def _collect_metrics(self, ev: InvestigationEvidence, ns: str | None, nodes: set[str]) -> None:
        if ns and ev.pods:
            pm = self._get(ev, "pod_metrics", namespace=ns)
            wanted = {p["name"] for p in ev.pods}
            if pm:
                ev.metrics["pods"] = {
                    item["metadata"]["name"]: {c["name"]: c.get("usage", {}) for c in item.get("containers", [])}
                    for item in pm.get("items", []) if item["metadata"]["name"] in wanted}
        if nodes or ev.nodes:
            nm = self._get(ev, "node_metrics")
            wanted = nodes | {n["name"] for n in ev.nodes}
            if nm:
                ev.metrics["nodes"] = {item["metadata"]["name"]: item.get("usage", {})
                                       for item in nm.get("items", []) if item["metadata"]["name"] in wanted}


# ---------------------------------------------------------------------- summaries (pure functions)

def clip(text: str, max_bytes: int) -> str:
    text = redact_text(text)
    if len(text) <= max_bytes:
        return text
    return "[... earlier lines cut ...]\n" + text[-max_bytes:]


def owner_deployment(engine: DiagnosticEngine, ev: InvestigationEvidence, pod: dict[str, Any], ns: str) -> str | None:
    for ref in pod["metadata"].get("ownerReferences") or []:
        if ref.get("kind") == "ReplicaSet":
            rs = engine._get(ev, "read_replicaset", name=ref["name"], namespace=ns)
            for rref in (rs or {}).get("metadata", {}).get("ownerReferences") or []:
                if rref.get("kind") == "Deployment":
                    return rref["name"]
    return None


def selector_string(deployment: dict[str, Any]) -> str:
    labels = ((deployment.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def pod_is_healthy(pod: dict[str, Any]) -> bool:
    status = pod.get("status") or {}
    if status.get("phase") != "Running":
        return False
    return all(c.get("ready") for c in status.get("containerStatuses") or [])


def _state(cs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    state = cs.get("state") or {}
    for k in ("waiting", "terminated", "running"):
        if state.get(k) is not None:
            return k, state[k] or {}
    return "unknown", {}


def summarize_pod(pod: dict[str, Any]) -> dict[str, Any]:
    meta, spec, status = pod["metadata"], pod.get("spec") or {}, pod.get("status") or {}
    statuses = {c["name"]: c for c in status.get("containerStatuses") or []}
    init_statuses = {c["name"]: c for c in status.get("initContainerStatuses") or []}
    containers = []
    for c in spec.get("containers") or []:
        cs = statuses.get(c["name"], {})
        kind, detail = _state(cs)
        last = (cs.get("lastState") or {}).get("terminated")
        containers.append({
            "name": c["name"],
            "image": c.get("image"),
            "ready": cs.get("ready", False),
            "restart_count": cs.get("restartCount", 0),
            "state": f"{kind}:{detail.get('reason', '')}" if kind != "running" else "running",
            "state_message": (detail.get("message") or "")[:500],
            "exit_code": detail.get("exitCode"),
            "last_terminated": {"reason": last.get("reason"), "exit_code": last.get("exitCode"),
                                "finished_at": last.get("finishedAt")} if last else None,
            "image_pull_failed": detail.get("reason") in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName"),
            "resources": c.get("resources") or {},
            "readiness_probe": probe_summary(c.get("readinessProbe")),
            "liveness_probe": probe_summary(c.get("livenessProbe")),
            "startup_probe": probe_summary(c.get("startupProbe")),
            "ports": [p.get("containerPort") for p in c.get("ports") or []],
            "env_names": [e["name"] for e in c.get("env") or []],
            "env_refs": [
                {"name": e["name"], **{k: v for k, v in (e.get("valueFrom") or {}).items()
                                       if k in ("secretKeyRef", "configMapKeyRef")}}
                for e in c.get("env") or [] if e.get("valueFrom")],
            "env_from": c.get("envFrom") or [],
        })
    init = []
    for c in spec.get("initContainers") or []:
        cs = init_statuses.get(c["name"], {})
        kind, detail = _state(cs)
        init.append({"name": c["name"], "image": c.get("image"), "command": c.get("command"), "args": c.get("args"),
                     "state": f"{kind}:{detail.get('reason', '')}", "restart_count": cs.get("restartCount", 0)})
    return {
        "name": meta["name"],
        "namespace": meta.get("namespace"),
        "labels": meta.get("labels") or {},
        "phase": status.get("phase"),
        "reason": status.get("reason"),
        "message": (status.get("message") or "")[:500],
        "node": spec.get("nodeName"),
        "node_selector": spec.get("nodeSelector") or {},
        "tolerations": [t for t in spec.get("tolerations") or []
                        if t.get("key") not in ("node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable")],
        "affinity": bool(spec.get("affinity")),
        "conditions": [{"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason"),
                        "message": (c.get("message") or "")[:300]} for c in status.get("conditions") or []],
        "containers": containers,
        "init_containers": init,
        "pvc_claims": [v["persistentVolumeClaim"]["claimName"] for v in spec.get("volumes") or []
                       if v.get("persistentVolumeClaim")],
        "volumes_from": sorted({k for v in spec.get("volumes") or [] for k in v if k != "name"}),
        "config_maps": sorted({v["configMap"]["name"] for v in spec.get("volumes") or [] if v.get("configMap")}
                              | {ef["configMapRef"]["name"] for c in spec.get("containers") or []
                                 for ef in c.get("envFrom") or [] if ef.get("configMapRef")}),
        "deletion_timestamp": meta.get("deletionTimestamp"),
        "created": meta.get("creationTimestamp"),
    }


def probe_summary(probe: dict[str, Any] | None) -> dict[str, Any] | None:
    if not probe:
        return None
    out = {k: probe.get(k) for k in ("initialDelaySeconds", "periodSeconds", "timeoutSeconds", "failureThreshold")
           if probe.get(k) is not None}
    if probe.get("httpGet"):
        out["httpGet"] = {"path": probe["httpGet"].get("path"), "port": probe["httpGet"].get("port")}
    if probe.get("tcpSocket"):
        out["tcpSocket"] = {"port": probe["tcpSocket"].get("port")}
    if probe.get("exec"):
        out["exec"] = True
    return out


def summarize_deployment(dep: dict[str, Any]) -> dict[str, Any]:
    spec, status = dep.get("spec") or {}, dep.get("status") or {}
    tpl = (spec.get("template") or {}).get("spec") or {}
    annotations = dep["metadata"].get("annotations") or {}
    return {
        "name": dep["metadata"]["name"],
        "namespace": dep["metadata"].get("namespace"),
        "labels": dep["metadata"].get("labels") or {},
        "managed_by_gitops": "argocd.argoproj.io/tracking-id" in annotations
                             or "app.kubernetes.io/instance" in (dep["metadata"].get("labels") or {}),
        "revision": annotations.get("deployment.kubernetes.io/revision"),
        "replicas": spec.get("replicas"),
        "strategy": spec.get("strategy"),
        "selector": (spec.get("selector") or {}).get("matchLabels"),
        "containers": [{"name": c["name"], "image": c.get("image"), "resources": c.get("resources") or {},
                        "readiness_probe": probe_summary(c.get("readinessProbe")),
                        "liveness_probe": probe_summary(c.get("livenessProbe"))} for c in tpl.get("containers") or []],
        "node_selector": tpl.get("nodeSelector") or {},
        "status": {k: status.get(k) for k in ("replicas", "readyReplicas", "availableReplicas",
                                              "unavailableReplicas", "updatedReplicas", "observedGeneration")},
        "conditions": [{"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason"),
                        "message": (c.get("message") or "")[:300]} for c in status.get("conditions") or []],
    }


def summarize_replicaset(rs: dict[str, Any]) -> dict[str, Any]:
    status = rs.get("status") or {}
    tpl = ((rs.get("spec") or {}).get("template") or {}).get("spec") or {}
    return {
        "name": rs["metadata"]["name"],
        "revision": (rs["metadata"].get("annotations") or {}).get("deployment.kubernetes.io/revision"),
        "replicas": (rs.get("spec") or {}).get("replicas"),
        "ready": status.get("readyReplicas", 0),
        "images": [c.get("image") for c in tpl.get("containers") or []],
        "conditions": [{"type": c.get("type"), "reason": c.get("reason"), "message": (c.get("message") or "")[:300]}
                       for c in status.get("conditions") or []],
        "created": rs["metadata"].get("creationTimestamp"),
    }


def summarize_node(node: dict[str, Any]) -> dict[str, Any]:
    spec, status = node.get("spec") or {}, node.get("status") or {}
    labels = node["metadata"].get("labels") or {}
    return {
        "name": node["metadata"]["name"],
        "unschedulable": bool(spec.get("unschedulable")),
        "taints": spec.get("taints") or [],
        "conditions": [{"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason"),
                        "message": (c.get("message") or "")[:300],
                        "last_heartbeat": c.get("lastHeartbeatTime"),
                        "last_transition": c.get("lastTransitionTime")} for c in status.get("conditions") or []],
        "allocatable": status.get("allocatable") or {},
        "capacity": status.get("capacity") or {},
        "kubelet_version": (status.get("nodeInfo") or {}).get("kubeletVersion"),
        "instance_type": labels.get("node.kubernetes.io/instance-type"),
        "zone": labels.get("topology.kubernetes.io/zone"),
        "provider_id": spec.get("providerID"),
        "compute_type": labels.get("eks.amazonaws.com/compute-type", "ec2"),
        "labels": {k: v for k, v in labels.items() if not k.startswith(("beta.kubernetes.io", "kubernetes.io/hostname"))},
    }


def summarize_pvc(pvc: dict[str, Any]) -> dict[str, Any]:
    spec, status = pvc.get("spec") or {}, pvc.get("status") or {}
    return {"name": pvc["metadata"]["name"], "phase": status.get("phase"),
            "storage_class": spec.get("storageClassName"), "access_modes": spec.get("accessModes"),
            "requested": ((spec.get("resources") or {}).get("requests") or {}).get("storage"),
            "volume": spec.get("volumeName")}


def is_default_sc(sc: dict[str, Any]) -> bool:
    ann = sc["metadata"].get("annotations") or {}
    return ann.get("storageclass.kubernetes.io/is-default-class") == "true"


def summarize_quota(q: dict[str, Any]) -> dict[str, Any]:
    status = q.get("status") or {}
    return {"name": q["metadata"]["name"], "hard": status.get("hard") or {}, "used": status.get("used") or {}}


def summarize_event(e: dict[str, Any]) -> dict[str, Any]:
    obj = e.get("involvedObject") or {}
    return {
        "type": e.get("type"),
        "reason": e.get("reason"),
        "object": f"{obj.get('kind')}/{obj.get('name')}",
        "message": (e.get("message") or "")[:600],
        "count": e.get("count") or 1,
        "source": (e.get("source") or {}).get("component") or e.get("reportingComponent"),
        "last_seen": e.get("lastTimestamp") or e.get("eventTime") or (e.get("metadata") or {}).get("creationTimestamp"),
    }


def dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in events:
        key = (e.get("object") or "", e.get("reason") or "", e.get("message") or "")
        seen[key] = e
    return sorted(seen.values(), key=lambda e: e.get("last_seen") or "")


def is_dns_alert(alert: NormalizedAlert) -> bool:
    name = alert.alertname.lower()
    return "dns" in name or "coredns" in name
