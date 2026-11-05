"""Configuration from environment variables and mounted files.

Secrets (SMTP password, webhook token, LLM API key, approver hashes) are read
from files when `<NAME>_FILE` is set, so they can come from a mounted
Kubernetes Secret instead of plain environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")


def env(name: str, default: str | None = None) -> str | None:
    """Value of `name`, or the contents of the file named by `name_FILE`."""
    file_var = os.environ.get(f"{name}_FILE")
    if file_var:
        try:
            return Path(file_var).read_text().strip()
        except OSError:
            return default
    return os.environ.get(name, default)


def env_int(name: str, default: int) -> int:
    value = env(name)
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def env_list(name: str, default: str = "") -> list[str]:
    return [v.strip() for v in (env(name, default) or "").split(",") if v.strip()]


@dataclass(frozen=True)
class ClusterConfig:
    """One cluster the agent may investigate, with its own credentials.

    in_cluster: use the agent's ServiceAccount (the cluster the agent runs in).
    kubeconfig/context: a kubeconfig holding a read-only token for another cluster.
    """

    name: str
    environment: str = "dev"
    in_cluster: bool = False
    kubeconfig: str | None = None
    context: str | None = None
    dashboard_url: str | None = None


@dataclass(frozen=True)
class Limits:
    log_tail_lines: int = 200
    log_max_bytes: int = 32_000
    max_events: int = 40
    max_pods: int = 5
    api_timeout_seconds: int = 10
    investigation_timeout_seconds: int = 60
    max_concurrent_investigations: int = 3


@dataclass(frozen=True)
class SmtpConfig:
    host: str | None = None
    port: int = 587
    username: str | None = None
    password: str | None = None
    sender: str | None = None
    starttls: bool = True
    timeout_seconds: int = 20

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.sender)


@dataclass(frozen=True)
class LlmConfig:
    provider: str = "none"  # none | anthropic | openai | bedrock
    model: str | None = None
    api_key: str | None = None
    region: str | None = None
    timeout_seconds: int = 60
    max_evidence_chars: int = 60_000


@dataclass
class Settings:
    db_path: str = "/data/sre-agent.db"
    clusters: dict[str, ClusterConfig] = field(default_factory=dict)
    default_cluster: str | None = None
    webhook_token: str | None = None
    approvers_file: str | None = None
    session_secret: str = ""
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    # "<environment>:<SEVERITY>", "<SEVERITY>" or "default" -> addresses
    recipients: dict[str, list[str]] = field(default_factory=dict)
    email_min_severity: str = "MEDIUM"
    email_dedup_minutes: int = 60
    approval_ttl_hours: int = 24
    dashboard_url: str | None = None
    critical_namespaces: list[str] = field(default_factory=list)
    rate_limit_per_minute: int = 120
    limits: Limits = field(default_factory=Limits)
    llm: LlmConfig = field(default_factory=LlmConfig)

    def cluster(self, name: str | None) -> ClusterConfig | None:
        """The configured cluster called `name` (or the default). Never guesses."""
        key = name or self.default_cluster
        return self.clusters.get(key) if key else None

    def recipients_for(self, environment: str, severity: str) -> list[str]:
        for key in (f"{environment}:{severity}", severity, "default"):
            if self.recipients.get(key):
                return self.recipients[key]
        return []

    @classmethod
    def from_env(cls) -> "Settings":
        clusters: dict[str, ClusterConfig] = {}
        clusters_file = env("SRE_AGENT_CLUSTERS_FILE")
        if clusters_file and Path(clusters_file).exists():
            data = yaml.safe_load(Path(clusters_file).read_text()) or {}
            for item in data.get("clusters", []):
                c = ClusterConfig(
                    name=item["name"],
                    environment=item.get("environment", "dev"),
                    in_cluster=bool(item.get("in_cluster", False)),
                    kubeconfig=item.get("kubeconfig"),
                    context=item.get("context"),
                    dashboard_url=item.get("dashboard_url"),
                )
                clusters[c.name] = c
        else:
            name = env("SRE_AGENT_CLUSTER_NAME", "local") or "local"
            clusters[name] = ClusterConfig(
                name=name,
                environment=env("SRE_AGENT_CLUSTER_ENV", "dev") or "dev",
                in_cluster=(env("SRE_AGENT_IN_CLUSTER", "true") or "true").lower() == "true",
                kubeconfig=env("SRE_AGENT_KUBECONFIG"),
                context=env("SRE_AGENT_KUBE_CONTEXT"),
            )

        recipients: dict[str, list[str]] = {}
        for key, value in os.environ.items():
            # SRE_AGENT_EMAIL_TO=..., SRE_AGENT_EMAIL_TO_CRITICAL=..., SRE_AGENT_EMAIL_TO_PROD_CRITICAL=...
            if not key.startswith("SRE_AGENT_EMAIL_TO"):
                continue
            suffix = key[len("SRE_AGENT_EMAIL_TO"):].strip("_")
            parts = suffix.split("_") if suffix else []
            if not parts:
                rkey = "default"
            elif len(parts) == 1:
                rkey = parts[0].upper()
            else:
                rkey = f"{'_'.join(parts[:-1]).lower()}:{parts[-1].upper()}"
            recipients[rkey] = [a.strip() for a in value.split(",") if a.strip()]

        return cls(
            db_path=env("SRE_AGENT_DB", "/data/sre-agent.db") or "/data/sre-agent.db",
            clusters=clusters,
            default_cluster=env("SRE_AGENT_DEFAULT_CLUSTER") or (next(iter(clusters)) if len(clusters) == 1 else None),
            webhook_token=env("SRE_AGENT_WEBHOOK_TOKEN"),
            approvers_file=env("SRE_AGENT_APPROVERS_FILE"),
            session_secret=env("SRE_AGENT_SESSION_SECRET", "") or "",
            smtp=SmtpConfig(
                host=env("SRE_AGENT_SMTP_HOST"),
                port=env_int("SRE_AGENT_SMTP_PORT", 587),
                username=env("SRE_AGENT_SMTP_USERNAME"),
                password=env("SRE_AGENT_SMTP_PASSWORD"),
                sender=env("SRE_AGENT_SMTP_FROM"),
                starttls=(env("SRE_AGENT_SMTP_STARTTLS", "true") or "true").lower() == "true",
            ),
            recipients=recipients,
            email_min_severity=(env("SRE_AGENT_EMAIL_MIN_SEVERITY", "MEDIUM") or "MEDIUM").upper(),
            email_dedup_minutes=env_int("SRE_AGENT_EMAIL_DEDUP_MINUTES", 60),
            approval_ttl_hours=env_int("SRE_AGENT_APPROVAL_TTL_HOURS", 24),
            dashboard_url=env("SRE_AGENT_DASHBOARD_URL"),
            critical_namespaces=env_list("SRE_AGENT_CRITICAL_NAMESPACES", "payments,core-banking"),
            rate_limit_per_minute=env_int("SRE_AGENT_RATE_LIMIT_PER_MINUTE", 120),
            limits=Limits(
                log_tail_lines=env_int("SRE_AGENT_LOG_TAIL_LINES", 200),
                log_max_bytes=env_int("SRE_AGENT_LOG_MAX_BYTES", 32_000),
                max_events=env_int("SRE_AGENT_MAX_EVENTS", 40),
                max_pods=env_int("SRE_AGENT_MAX_PODS", 5),
                api_timeout_seconds=env_int("SRE_AGENT_API_TIMEOUT_SECONDS", 10),
                investigation_timeout_seconds=env_int("SRE_AGENT_INVESTIGATION_TIMEOUT_SECONDS", 60),
                max_concurrent_investigations=env_int("SRE_AGENT_MAX_CONCURRENT", 3),
            ),
            llm=LlmConfig(
                provider=(env("SRE_AGENT_LLM_PROVIDER", "none") or "none").lower(),
                model=env("SRE_AGENT_LLM_MODEL"),
                api_key=env("SRE_AGENT_LLM_API_KEY"),
                region=env("SRE_AGENT_LLM_REGION") or env("AWS_REGION"),
                timeout_seconds=env_int("SRE_AGENT_LLM_TIMEOUT_SECONDS", 60),
            ),
        )
