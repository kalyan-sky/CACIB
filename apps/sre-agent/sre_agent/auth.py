"""Authentication for the dashboard and the approval API, webhook token check, rate limiting.

Users live in an approvers file (mounted from a Kubernetes Secret), one per line:

    <username>:<role>:pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>

role is `approver` (may approve/reject) or `viewer` (read only). Generate a line with
    python -m sre_agent.auth <username> <role>
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

ITERATIONS = 310_000
ROLES = ("viewer", "approver")


@dataclass(frozen=True)
class User:
    name: str
    role: str

    @property
    def can_approve(self) -> bool:
        return self.role == "approver"


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt, digest = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


class UserDirectory:
    def __init__(self, path: str | None):
        self.path = path
        self._users: dict[str, tuple[str, str]] = {}
        self._mtime = 0.0
        self._lock = threading.Lock()

    def _load(self) -> None:
        if not self.path or not Path(self.path).exists():
            self._users = {}
            return
        mtime = Path(self.path).stat().st_mtime
        if mtime == self._mtime:
            return
        users: dict[str, tuple[str, str]] = {}
        for line in Path(self.path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[1] in ROLES:
                users[parts[0]] = (parts[1], parts[2])
        self._users, self._mtime = users, mtime

    def authenticate(self, username: str, password: str) -> User | None:
        with self._lock:
            self._load()
            entry = self._users.get(username)
        if not entry:
            hash_password("timing-equaliser", 1000)
            return None
        role, encoded = entry
        return User(username, role) if verify_password(password, encoded) else None

    @property
    def configured(self) -> bool:
        with self._lock:
            self._load()
            return bool(self._users)


def token_matches(given: str | None, expected: str | None) -> bool:
    return bool(given and expected) and hmac.compare_digest(given.encode(), (expected or "").encode())


def csrf_token(secret: str, user: str, purpose: str) -> str:
    """Form token tied to the user and the approval: a cross-site form cannot produce it."""
    return hmac.new(secret.encode(), f"{user}|{purpose}".encode(), hashlib.sha256).hexdigest()


class RateLimiter:
    """Sliding window per client key (IP or user)."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        t = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] < t - 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(t)
            return True


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[2] not in ROLES:
        sys.exit("usage: python -m sre_agent.auth <username> <viewer|approver>")
    pw = getpass.getpass("password: ")
    if len(pw) < 12:
        sys.exit("use at least 12 characters")
    print(f"{sys.argv[1]}:{sys.argv[2]}:{hash_password(pw)}")
