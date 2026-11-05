from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sre_agent.auth import hash_password  # noqa: E402
from sre_agent.config import ClusterConfig, Limits, Settings, SmtpConfig  # noqa: E402
from sre_agent.store import Store  # noqa: E402


class FakeSMTP:
    """Stands in for smtplib.SMTP; records what would have been sent."""

    sent: list[Any] = []
    fail = False

    def __init__(self, host: str, port: int, timeout: int = 0):
        self.host, self.port = host, port

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *a: Any) -> None:
        pass

    def starttls(self, context: Any = None) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        self.user = user

    def send_message(self, msg: Any) -> None:
        if FakeSMTP.fail:
            import smtplib

            raise smtplib.SMTPServerDisconnected("connection lost")
        FakeSMTP.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.sent = []
    FakeSMTP.fail = False
    monkeypatch.setattr("sre_agent.notify.email.smtplib.SMTP", FakeSMTP)
    return FakeSMTP


@pytest.fixture
def approvers_file(tmp_path: Path) -> str:
    p = tmp_path / "approvers"
    p.write_text(f"alice:approver:{hash_password('alice-password-123', 1000)}\n"
                 f"victor:viewer:{hash_password('victor-password-123', 1000)}\n")
    return str(p)


@pytest.fixture
def settings(approvers_file: str) -> Settings:
    return Settings(
        db_path=":memory:",
        clusters={"test-eks": ClusterConfig(name="test-eks", environment="prod"),
                  "uat-eks": ClusterConfig(name="uat-eks", environment="uat")},
        default_cluster=None,
        webhook_token="webhook-token-0123456789",
        approvers_file=approvers_file,
        session_secret="test-secret",
        smtp=SmtpConfig(host="smtp.example", port=587, username="bot@example.com", password="app-password",
                        sender="bot@example.com"),
        recipients={"default": ["oncall@example.com"], "prod:CRITICAL": ["oncall@example.com", "head-sre@example.com"]},
        critical_namespaces=["payments"],
        limits=Limits(investigation_timeout_seconds=20),
    )


@pytest.fixture
def store() -> Store:
    return Store(":memory:")
