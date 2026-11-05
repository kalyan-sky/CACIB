"""Data shapes: the Alertmanager webhook, the normalised alert, and the incident report."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .config import SEVERITIES

# ---------------------------------------------------------------- Alertmanager webhook (version 4)

_LABEL_VALUE_MAX = 1024


class AmAlert(BaseModel):
    status: Literal["firing", "resolved"]
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str | None = None
    endsAt: str | None = None
    generatorURL: str | None = None
    fingerprint: str = Field(default="", max_length=64)

    @field_validator("labels", "annotations")
    @classmethod
    def _bounded(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 64:
            raise ValueError("too many labels")
        return {str(k)[:128]: str(val)[:_LABEL_VALUE_MAX] for k, val in v.items()}


class AmWebhook(BaseModel):
    version: str = "4"
    groupKey: str | None = None
    status: Literal["firing", "resolved"] = "firing"
    receiver: str | None = None
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    externalURL: str | None = None
    alerts: list[AmAlert] = Field(default_factory=list, max_length=200)


# ---------------------------------------------------------------- normalised alert

# Kubernetes generates name suffixes from an alphabet without vowels (and 0, 1, 3),
# so "exporter" in node-exporter-x7k2p is never mistaken for a hash.
_SAFE = "[bcdfghjklmnpqrstvwxz2456789]"
_DEPLOY_POD = re.compile(rf"^(?P<base>.+)-{_SAFE}{{6,10}}-{_SAFE}{{5}}$")  # <deploy>-<rs hash>-<pod>
_OTHER_POD = re.compile(rf"^(?P<base>.+)-{_SAFE}{{5}}$")  # DaemonSet / Job pods
_K8S_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")


def _k8s_name(value: str | None) -> str | None:
    """A label value is used as a Kubernetes name only if it is a valid one."""
    return value if value and _K8S_NAME.match(value) else None


class NormalizedAlert(BaseModel):
    cluster: str
    environment: str
    alertname: str
    status: str
    severity: str
    fingerprint: str
    namespace: str | None = None
    pod: str | None = None
    container: str | None = None
    deployment: str | None = None
    node: str | None = None
    pvc: str | None = None
    service: str | None = None
    summary: str = ""
    description: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    starts_at: str | None = None

    @property
    def workload(self) -> str | None:
        """Deployment name, or a pod name without its ReplicaSet/pod hash."""
        if self.deployment:
            return self.deployment
        if self.pod:
            m = _DEPLOY_POD.match(self.pod) or _OTHER_POD.match(self.pod)
            return m.group("base") if m else self.pod
        return None

    @property
    def correlation_key(self) -> str:
        """Alerts about the same thing share one incident (e.g. CrashLooping + ReplicasMismatch)."""
        if self.node and not self.pod:
            target = f"node/{self.node}"
        elif self.pvc:
            target = f"pvc/{self.namespace}/{self.pvc}"
        elif self.workload:
            target = f"workload/{self.namespace}/{self.workload}"
        else:
            target = f"alert/{self.alertname}/{self.namespace or '-'}"
        return f"{self.cluster}|{target}"

    @classmethod
    def from_alert(cls, alert: AmAlert, cluster: str, environment: str,
                   critical_namespaces: list[str]) -> "NormalizedAlert":
        lbl = alert.labels
        node = lbl.get("node") or (lbl.get("instance") if lbl.get("job") == "node-exporter" else None)
        if node and ":" in node:
            node = None  # an IP:port, not a node name
        return cls(
            cluster=cluster,
            environment=environment,
            alertname=lbl.get("alertname", "unknown")[:128],
            status=alert.status,
            severity=map_severity(lbl, critical_namespaces),
            fingerprint=alert.fingerprint or "",
            namespace=_k8s_name(lbl.get("namespace") or lbl.get("exported_namespace")),
            pod=_k8s_name(lbl.get("pod")),
            container=_k8s_name(lbl.get("container")),
            deployment=_k8s_name(lbl.get("deployment")),
            node=_k8s_name(node),
            pvc=_k8s_name(lbl.get("persistentvolumeclaim")),
            service=_k8s_name(lbl.get("service")) if lbl.get("job") != lbl.get("service") else None,
            summary=alert.annotations.get("summary", "")[:500],
            description=alert.annotations.get("description", alert.annotations.get("message", ""))[:1000],
            labels=lbl,
            starts_at=alert.startsAt,
        )


def map_severity(labels: dict[str, str], critical_namespaces: list[str]) -> str:
    """Prometheus severity labels -> the agent's five levels.

    An explicit `sre_severity` label wins. `warning` becomes HIGH in the
    business-critical namespaces and MEDIUM elsewhere.
    """
    explicit = labels.get("sre_severity", "").upper()
    if explicit in SEVERITIES:
        return explicit
    sev = labels.get("severity", "").lower()
    if sev == "critical":
        return "CRITICAL"
    if sev in ("warning", "high"):
        return "HIGH" if labels.get("namespace") in critical_namespaces else "MEDIUM"
    if sev in ("low", "minor"):
        return "LOW"
    if sev in ("info", "none"):
        return "INFO"
    return "MEDIUM"


def severity_rank(severity: str) -> int:
    """CRITICAL=0 ... INFO=4; unknown sorts last."""
    return SEVERITIES.index(severity) if severity in SEVERITIES else len(SEVERITIES)


# ---------------------------------------------------------------- incident report

CommandKind = Literal["diagnostic", "remediation", "verification", "rollback"]

KIND_LABELS = {
    "diagnostic": "Diagnostic command (read-only)",
    "remediation": "Proposed remediation command (NOT executed)",
    "verification": "Verification command (read-only)",
    "rollback": "Rollback command (NOT executed)",
}


class Command(BaseModel):
    kind: CommandKind
    command: str
    explanation: str = ""
    expected_output: str = ""
    executed_by_agent: bool = False  # always False for remediation/rollback

    @property
    def label(self) -> str:
        return KIND_LABELS[self.kind]


class ResolutionStep(BaseModel):
    title: str
    explanation: str = ""
    commands: list[Command] = Field(default_factory=list)


class Evidence(BaseModel):
    """One observed fact, with where it came from. Hypotheses never go here."""

    source: str  # e.g. "pod.status", "events", "logs.previous", "node.conditions"
    observation: str


INCONCLUSIVE = "Root cause not conclusively identified. Additional investigation is required."


class IncidentReport(BaseModel):
    incident_id: str
    severity: str
    cluster: str
    namespace: str | None = None
    affected_resources: list[str] = Field(default_factory=list)
    incident_type: str = "unknown"
    incident_summary: str = ""
    symptoms: list[str] = Field(default_factory=list)
    root_cause: str = INCONCLUSIVE
    root_cause_status: Literal["confirmed", "probable", "inconclusive"] = "inconclusive"
    root_cause_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    supporting_evidence: list[Evidence] = Field(default_factory=list)
    possible_contributing_factors: list[str] = Field(default_factory=list)  # hypotheses
    business_impact: str = ""
    recommended_investigations: list[Command] = Field(default_factory=list)
    resolution_steps: list[ResolutionStep] = Field(default_factory=list)
    commands_for_human_execution: list[Command] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    expected_outcome: str = ""
    verification_steps: list[Command] = Field(default_factory=list)
    rollback_guidance: list[Command] = Field(default_factory=list)
    risk_assessment: str = ""
    requires_human_approval: bool = True
    agent_actions_taken: list[str] = Field(default_factory=list)
    analysis_engine: str = "rules"
    analysis_notes: list[str] = Field(default_factory=list)
    started_at: str | None = None
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @field_validator("severity")
    @classmethod
    def _sev(cls, v: str) -> str:
        v = v.upper()
        if v not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}")
        return v

    def enforce_invariants(self) -> "IncidentReport":
        """Rules no analysis engine may change: humans approve, the agent never executes fixes."""
        self.requires_human_approval = True
        for cmd in self.commands_for_human_execution + self.rollback_guidance:
            cmd.executed_by_agent = False
        for step in self.resolution_steps:
            for cmd in step.commands:
                if cmd.kind in ("remediation", "rollback"):
                    cmd.executed_by_agent = False
        if not self.supporting_evidence:
            self.root_cause_status = "inconclusive"
            self.root_cause_confidence = min(self.root_cause_confidence, 0.3)
        if self.root_cause_status == "inconclusive" and INCONCLUSIVE not in self.root_cause:
            self.root_cause = f"{INCONCLUSIVE} {self.root_cause}".strip()
        return self


class InvestigationEvidence(BaseModel):
    """Everything the diagnostic engine collected, already redacted."""

    cluster: str
    namespace: str | None = None
    targets: dict[str, Any] = Field(default_factory=dict)  # resolved pod/deployment/node/...
    pods: list[dict[str, Any]] = Field(default_factory=list)
    deployment: dict[str, Any] | None = None
    replicasets: list[dict[str, Any]] = Field(default_factory=list)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    logs: dict[str, str] = Field(default_factory=dict)  # "<pod>/<container>[/previous]" -> text
    metrics: dict[str, Any] = Field(default_factory=dict)
    services: list[dict[str, Any]] = Field(default_factory=list)
    pvcs: list[dict[str, Any]] = Field(default_factory=list)
    storage_classes: list[str] = Field(default_factory=list)
    quotas: list[dict[str, Any]] = Field(default_factory=list)
    coredns: dict[str, Any] | None = None
    commands_executed: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    truncated: bool = False
    duration_seconds: float = 0.0
