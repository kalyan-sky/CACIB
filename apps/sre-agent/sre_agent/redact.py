"""Remove secrets and credentials from evidence before it is stored, emailed or sent to an LLM."""

from __future__ import annotations

import re
from typing import Any

MASK = "[REDACTED]"

# Environment variable / key names whose values are treated as secret.
_SECRET_NAME = re.compile(r"(pass(word|wd)?|secret|token|api[_-]?key|private[_-]?key|credential|auth|cookie|session)",
                          re.IGNORECASE)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # scheme://user:password@host -> scheme://user:[REDACTED]@host
    (re.compile(r"(?P<pre>[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(?P<post>@)", re.IGNORECASE), rf"\g<pre>{MASK}\g<post>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), MASK),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), MASK),  # AWS access key id
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"), MASK),  # GitLab token
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), MASK),  # GitHub token
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), MASK),  # OpenAI/Anthropic-style API key
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"), MASK),  # JWT
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"), rf"\1 {MASK}"),
    # key=value / key: value / "key": "value" where the key looks secret
    (re.compile(r"""(?ix)
        (?P<key>["']?[\w.\-]*(?:pass(?:word|wd)?|secret|token|api[_-]?key|private[_-]?key|credential)[\w.\-]*["']?)
        (?P<sep>\s*[:=]\s*)
        (?P<q>["']?)(?P<val>[^\s"',;&}]+)(?P=q)"""), rf"\g<key>\g<sep>\g<q>{MASK}\g<q>"),
]


def redact_text(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact(obj: Any) -> Any:
    """Redact a JSON-like structure: strings by pattern, env values by variable name."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        # {"name": "DB_PASSWORD", "value": "..."} (container env)
        if "name" in obj and "value" in obj and isinstance(obj.get("name"), str) and _SECRET_NAME.search(obj["name"]):
            out = dict(obj)
            out["value"] = MASK
            return out
        for k, v in obj.items():
            if k in ("managedFields",):
                continue
            if k == "annotations" and isinstance(v, dict):
                # last-applied-configuration repeats the whole object, env values included
                v = {ak: (MASK if "last-applied" in ak else av) for ak, av in v.items()}
            if isinstance(v, str) and isinstance(k, str) and _SECRET_NAME.search(k) and k not in ("secretName", "secretKeyRef"):
                out[k] = MASK
            else:
                out[k] = redact(v)
        return out
    return obj
