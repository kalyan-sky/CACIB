"""Optional LLM analysis on top of the rule-based report.

Off by default (SRE_AGENT_LLM_PROVIDER=none). When a provider is configured,
the LLM receives the structured, redacted evidence plus the rule-based
finding, and must answer with JSON matching IncidentReport. Its answer is
validated; the safety invariants are re-applied; every evidence item must
quote something that really is in the collected evidence. Any failure keeps
the rule-based report, so an LLM outage never loses or blocks an incident.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Protocol

import httpx

from .. import metrics
from ..config import LlmConfig
from ..models import Evidence, IncidentReport, InvestigationEvidence

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a senior Kubernetes SRE. You analyse an incident from evidence collected by a
read-only diagnostic engine. Rules:
- Use ONLY the evidence provided. Never invent log lines, events, values or resources.
- Every item in supporting_evidence must quote or closely paraphrase a specific observation in the
  evidence, with "source" naming where it came from (pod.status, events, logs.previous, node.conditions,
  metrics, deployment.status, services, pvc, resourcequota).
- Put anything you infer but cannot see in possible_contributing_factors, marked "(hypothesis)".
- If the evidence does not establish a cause, set root_cause_status "inconclusive", root_cause to
  "Root cause not conclusively identified. Additional investigation is required." and confidence <= 0.3.
- Commands are recommendations for a human. kind is one of diagnostic, remediation, verification, rollback.
  Never claim the agent executed a remediation. requires_human_approval is always true.
- Answer with a single JSON object matching the schema of the draft report you are given. No prose outside JSON."""


class LlmProvider(Protocol):
    name: str

    def complete(self, system: str, user: str) -> str: ...


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, cfg: LlmConfig):
        if not cfg.api_key:
            raise ValueError("SRE_AGENT_LLM_API_KEY is required for the anthropic provider")
        self.cfg = cfg
        self.model = cfg.model or "claude-sonnet-5-5"

    def complete(self, system: str, user: str) -> str:
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=self.cfg.timeout_seconds,
                       headers={"x-api-key": self.cfg.api_key or "", "anthropic-version": "2023-06-01",
                                "content-type": "application/json"},
                       json={"model": self.model, "max_tokens": 4096, "system": system,
                             "messages": [{"role": "user", "content": user}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")


class OpenAIProvider:
    name = "openai"

    def __init__(self, cfg: LlmConfig):
        if not cfg.api_key:
            raise ValueError("SRE_AGENT_LLM_API_KEY is required for the openai provider")
        self.cfg = cfg
        self.model = cfg.model or "gpt-4o"

    def complete(self, system: str, user: str) -> str:
        r = httpx.post("https://api.openai.com/v1/chat/completions", timeout=self.cfg.timeout_seconds,
                       headers={"authorization": f"Bearer {self.cfg.api_key}"},
                       json={"model": self.model, "response_format": {"type": "json_object"},
                             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


class BedrockProvider:
    """Amazon Bedrock Converse API. Needs boto3 in the image and bedrock:InvokeModel for the node/pod role."""

    name = "bedrock"

    def __init__(self, cfg: LlmConfig):
        import boto3  # optional dependency

        self.cfg = cfg
        self.model = cfg.model or "anthropic.claude-sonnet-5-5"
        self.client = boto3.client("bedrock-runtime", region_name=cfg.region)

    def complete(self, system: str, user: str) -> str:
        out = self.client.converse(modelId=self.model, system=[{"text": system}],
                                   messages=[{"role": "user", "content": [{"text": user}]}],
                                   inferenceConfig={"maxTokens": 4096})
        return "".join(c.get("text", "") for c in out["output"]["message"]["content"])


def make_provider(cfg: LlmConfig) -> LlmProvider | None:
    if cfg.provider in ("", "none", "rules"):
        return None
    providers = {"anthropic": AnthropicProvider, "openai": OpenAIProvider, "bedrock": BedrockProvider}
    if cfg.provider not in providers:
        raise ValueError(f"unknown LLM provider '{cfg.provider}' (use none, anthropic, openai or bedrock)")
    return providers[cfg.provider](cfg)


def _extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the LLM answer")
    return json.loads(text[start:end + 1])


def _grounded(item: Evidence, evidence_text: str) -> bool:
    """Is this evidence item backed by the collected evidence? (longest quoted fragment must appear)"""
    words = [w for w in item.observation.replace('"', " ").split() if len(w) > 3]
    if not words:
        return False
    hits = sum(1 for w in words if w.lower().strip(".,:;()'") in evidence_text)
    return hits / len(words) >= 0.6


class LlmAnalyzer:
    def __init__(self, provider: LlmProvider | None, cfg: LlmConfig):
        self.provider = provider
        self.cfg = cfg

    def refine(self, draft: IncidentReport, ev: InvestigationEvidence) -> IncidentReport:
        if not self.provider:
            return draft
        evidence_json = ev.model_dump_json(exclude={"commands_executed"})[: self.cfg.max_evidence_chars]
        user = ("EVIDENCE (redacted, collected read-only):\n" + evidence_json +
                "\n\nDRAFT REPORT from the rule engine (improve it, keep the same JSON schema):\n" +
                draft.model_dump_json())
        start = time.monotonic()
        try:
            raw = self.provider.complete(SYSTEM_PROMPT, user)
            data = _extract_json(raw)
            # Fields the LLM may not change
            for key in ("incident_id", "cluster", "namespace", "agent_actions_taken", "started_at"):
                data[key] = getattr(draft, key)
            data["severity"] = data.get("severity", draft.severity)
            report = IncidentReport.model_validate(data)
            text = evidence_json.lower()
            kept = [e for e in report.supporting_evidence if _grounded(e, text)]
            dropped = len(report.supporting_evidence) - len(kept)
            report.supporting_evidence = kept
            if dropped:
                report.analysis_notes.append(f"{dropped} LLM evidence item(s) dropped: not found in the collected evidence")
                report.root_cause_confidence = min(report.root_cause_confidence, draft.root_cause_confidence or 0.5)
            report.analysis_engine = f"llm:{self.provider.name}"
            report.analysis_notes = draft.analysis_notes + report.analysis_notes
            return report.enforce_invariants()
        except Exception as e:
            metrics.AI_FAILURES.labels(self.provider.name).inc()
            log.warning("LLM analysis failed, keeping the rule-based report", extra={"error": str(e)[:300]})
            draft.analysis_notes.append(f"LLM analysis failed ({type(e).__name__}); rule-based report kept")
            return draft
        finally:
            metrics.ANALYSIS_SECONDS.labels(f"llm:{self.provider.name}").observe(time.monotonic() - start)
