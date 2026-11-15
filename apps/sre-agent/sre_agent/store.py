"""SQLite persistence: incidents, approvals, the audit log and email de-duplication.

An incident is written before any analysis or email is attempted, so a failure
later in the pipeline never loses it.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE,
    cluster TEXT NOT NULL,
    environment TEXT,
    namespace TEXT,
    correlation_key TEXT NOT NULL,
    alertnames TEXT NOT NULL DEFAULT '[]',
    severity TEXT NOT NULL,
    incident_type TEXT DEFAULT 'unknown',
    status TEXT NOT NULL,              -- investigating | open | resolved | failed
    occurrences INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    alert_json TEXT,
    evidence_json TEXT,
    report_json TEXT,
    email_status TEXT DEFAULT 'pending',  -- pending | sent | skipped | failed | deduplicated
    email_attempts INTEGER NOT NULL DEFAULT 0,
    email_error TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_incidents_key ON incidents (correlation_key, status);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    proposed_action TEXT NOT NULL,
    evidence_summary TEXT,
    risk_assessment TEXT,
    instructions TEXT,
    status TEXT NOT NULL DEFAULT 'Pending',   -- Pending | Approved | Rejected | Expired
    approver TEXT,
    decided_at TEXT,
    comment TEXT,
    rejection_reason TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    incident_id TEXT,
    details TEXT
);
CREATE TABLE IF NOT EXISTS rca_notes (     -- the parts of the RCA only humans can write
    incident_id TEXT PRIMARY KEY REFERENCES incidents(id),
    actions_taken TEXT NOT NULL DEFAULT '',
    root_cause_confirmation TEXT NOT NULL DEFAULT '',
    lessons TEXT NOT NULL DEFAULT '',
    follow_ups TEXT NOT NULL DEFAULT '',
    updated_by TEXT,
    updated_at TEXT,
    signed_off_by TEXT,
    signed_off_at TEXT,
    agent_verdict TEXT,                -- correct | partial | incorrect: was the agent's diagnosis right?
    actual_cause_type TEXT             -- the real incident type when the agent was wrong
);
CREATE TABLE IF NOT EXISTS email_dedup (
    dedup_key TEXT PRIMARY KEY,
    incident_id TEXT,
    sent_at TEXT NOT NULL
);
"""

APPROVAL_STATES = ("Pending", "Approved", "Rejected", "Expired")


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL" if path != ":memory:" else "PRAGMA journal_mode=MEMORY")
            self._db.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was created (the volume outlives image upgrades)."""
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(rca_notes)")}
        for column in ("agent_verdict", "actual_cause_type"):
            if column not in have:
                self._db.execute(f"ALTER TABLE rca_notes ADD COLUMN {column} TEXT")

    def _q(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple[Any, ...] = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            return cur.lastrowid or cur.rowcount

    # ------------------------------------------------------------------ incidents

    def open_or_attach(self, *, cluster: str, environment: str, namespace: str | None, correlation_key: str,
                       alertname: str, severity: str, alert: dict[str, Any]) -> tuple[str, bool]:
        """Return (incident_id, created). Alerts about the same thing attach to the open incident."""
        with self._lock:
            row = self._db.execute(
                "SELECT id, alertnames FROM incidents WHERE correlation_key=? AND status IN ('investigating','open') "
                "ORDER BY seq DESC LIMIT 1", (correlation_key,)).fetchone()
            if row:
                names = json.loads(row["alertnames"])
                if alertname not in names:
                    names.append(alertname)
                self._db.execute("UPDATE incidents SET occurrences=occurrences+1, updated_at=?, alertnames=? WHERE id=?",
                                 (iso(), json.dumps(names), row["id"]))
                return row["id"], False
            ts = iso()
            cur = self._db.execute(
                "INSERT INTO incidents (cluster, environment, namespace, correlation_key, alertnames, severity, status,"
                " created_at, updated_at, alert_json) VALUES (?,?,?,?,?,?,'investigating',?,?,?)",
                (cluster, environment, namespace, correlation_key, json.dumps([alertname]), severity, ts, ts,
                 json.dumps(alert)))
            incident_id = f"INC-{cur.lastrowid:04d}"
            self._db.execute("UPDATE incidents SET id=? WHERE seq=?", (incident_id, cur.lastrowid))
            return incident_id, True

    def save_investigation(self, incident_id: str, evidence: dict[str, Any], report: dict[str, Any]) -> None:
        # an alert can resolve while the investigation runs: keep 'resolved'
        self._x("UPDATE incidents SET status=CASE WHEN status='resolved' THEN 'resolved' ELSE 'open' END, "
                "evidence_json=?, report_json=?, severity=?, incident_type=?, "
                "updated_at=? WHERE id=?",
                (json.dumps(evidence), json.dumps(report), report.get("severity"), report.get("incident_type"),
                 iso(), incident_id))

    def mark_failed(self, incident_id: str, error: str) -> None:
        self._x("UPDATE incidents SET status='failed', error=?, updated_at=? WHERE id=?", (error[:2000], iso(), incident_id))

    def resolve(self, correlation_key: str) -> list[str]:
        rows = self._q("SELECT id FROM incidents WHERE correlation_key=? AND status IN ('investigating','open','failed')",
                       (correlation_key,))
        ts = iso()
        for r in rows:
            self._x("UPDATE incidents SET status='resolved', resolved_at=?, updated_at=? WHERE id=?", (ts, ts, r["id"]))
        return [r["id"] for r in rows]

    def set_email_status(self, incident_id: str, status: str, error: str | None = None) -> None:
        self._x("UPDATE incidents SET email_status=?, email_error=?, email_attempts=email_attempts+1, updated_at=? "
                "WHERE id=?", (status, (error or "")[:1000] or None, iso(), incident_id))

    def emails_to_retry(self, max_attempts: int = 5) -> list[str]:
        return [r["id"] for r in self._q("SELECT id FROM incidents WHERE email_status='failed' AND email_attempts<? "
                                         "AND report_json IS NOT NULL", (max_attempts,))]

    def get(self, incident_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM incidents WHERE id=?", (incident_id,))
        return _incident(rows[0]) if rows else None

    def list(self, *, cluster: str | None = None, namespace: str | None = None, severity: str | None = None,
             incident_type: str | None = None, status: str | None = None, since: str | None = None,
             until: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM incidents WHERE 1=1", []
        for col, val in (("cluster", cluster), ("namespace", namespace), ("severity", severity),
                         ("incident_type", incident_type), ("status", status)):
            if val:
                sql += f" AND {col}=?"
                args.append(val)
        if since:
            sql += " AND created_at>=?"
            args.append(since)
        if until:
            sql += " AND created_at<=?"
            args.append(until)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(min(max(limit, 1), 1000))
        return [_incident(r) for r in self._q(sql, tuple(args))]

    def stats(self) -> dict[str, Any]:
        def count(where: str = "1=1", args: tuple[Any, ...] = ()) -> int:
            return self._q(f"SELECT COUNT(*) AS n FROM incidents WHERE {where}", args)[0]["n"]

        active = "status IN ('investigating','open','failed')"
        by_cluster = {r["cluster"]: r["n"] for r in self._q(
            f"SELECT cluster, COUNT(*) AS n FROM incidents WHERE {active} GROUP BY cluster")}
        by_ns = {f"{r['cluster']}/{r['namespace']}": r["n"] for r in self._q(
            f"SELECT cluster, namespace, COUNT(*) AS n FROM incidents WHERE {active} GROUP BY cluster, namespace "
            "ORDER BY n DESC LIMIT 20")}
        return {
            "total": count(),
            "critical": count(f"severity='CRITICAL' AND {active}"),
            "high": count(f"severity='HIGH' AND {active}"),
            "active": count(active),
            "resolved": count("status='resolved'"),
            "pending_approvals": self._q("SELECT COUNT(*) AS n FROM approvals WHERE status='Pending'")[0]["n"],
            "active_by_cluster": by_cluster,
            "active_by_namespace": by_ns,
        }

    # ------------------------------------------------------------------ approvals

    def create_approval(self, incident_id: str, proposed_action: str, evidence_summary: str, risk: str,
                        instructions: str, ttl_hours: int) -> int:
        existing = self._q("SELECT id FROM approvals WHERE incident_id=? AND status='Pending'", (incident_id,))
        if existing:
            return existing[0]["id"]
        return self._x("INSERT INTO approvals (incident_id, proposed_action, evidence_summary, risk_assessment, "
                       "instructions, created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
                       (incident_id, proposed_action, evidence_summary, risk, instructions, iso(),
                        iso(now() + timedelta(hours=ttl_hours))))

    def expire_approvals(self) -> int:
        rows = self._q("SELECT id, incident_id FROM approvals WHERE status='Pending' AND expires_at<?", (iso(),))
        for r in rows:
            self._x("UPDATE approvals SET status='Expired', decided_at=? WHERE id=?", (iso(), r["id"]))
            self.audit("system", "approval.expired", r["incident_id"], {"approval_id": r["id"]})
        return len(rows)

    def decide(self, approval_id: int, *, approved: bool, approver: str, comment: str = "",
               reason: str = "") -> dict[str, Any]:
        """Record a decision. This only changes the record: it never acts on a cluster."""
        with self._lock:
            self.expire_approvals()
            rows = self._q("SELECT * FROM approvals WHERE id=?", (approval_id,))
            if not rows:
                raise KeyError("approval not found")
            row = dict(rows[0])
            if row["status"] != "Pending":
                raise ValueError(f"approval is already {row['status']}")
            status = "Approved" if approved else "Rejected"
            self._x("UPDATE approvals SET status=?, approver=?, decided_at=?, comment=?, rejection_reason=? WHERE id=?",
                    (status, approver, iso(), comment[:2000], (reason[:2000] if not approved else None), approval_id))
            self.audit(approver, f"approval.{status.lower()}", row["incident_id"],
                       {"approval_id": approval_id, "comment": comment[:500], "reason": reason[:500]})
            return dict(self._q("SELECT * FROM approvals WHERE id=?", (approval_id,))[0])

    def approvals(self, *, status: str | None = None, incident_id: str | None = None) -> list[dict[str, Any]]:
        self.expire_approvals()
        sql, args = "SELECT * FROM approvals WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        if incident_id:
            sql += " AND incident_id=?"
            args.append(incident_id)
        return [dict(r) for r in self._q(sql + " ORDER BY id DESC LIMIT 500", tuple(args))]

    def pending_approval_count(self) -> int:
        return self._q("SELECT COUNT(*) AS n FROM approvals WHERE status='Pending'")[0]["n"]

    # ------------------------------------------------------------------ RCA notes (human input)

    RCA_FIELDS = ("actions_taken", "root_cause_confirmation", "lessons", "follow_ups", "agent_verdict",
                  "actual_cause_type")
    VERDICTS = ("correct", "partial", "incorrect")

    def rca_notes(self, incident_id: str) -> dict[str, Any]:
        rows = self._q("SELECT * FROM rca_notes WHERE incident_id=?", (incident_id,))
        return dict(rows[0]) if rows else {k: "" for k in self.RCA_FIELDS}

    def save_rca_notes(self, incident_id: str, user: str, fields: dict[str, str], sign_off: bool = False) -> dict[str, Any]:
        with self._lock:
            if self.rca_notes(incident_id).get("signed_off_by"):
                raise ValueError("the RCA is already signed off")
            values = {k: (fields.get(k) or "")[:5000] for k in self.RCA_FIELDS}
            if values["agent_verdict"] and values["agent_verdict"] not in self.VERDICTS:
                raise ValueError(f"agent verdict must be one of {self.VERDICTS}")
            if values["agent_verdict"] == "correct":
                values["actual_cause_type"] = ""
            if sign_off and not values["actions_taken"].strip():
                raise ValueError("describe the actions taken before signing off")
            if sign_off and not values["agent_verdict"]:
                raise ValueError("say whether the agent's diagnosis was correct before signing off")
            if sign_off and values["agent_verdict"] == "incorrect" and not values["root_cause_confirmation"].strip():
                raise ValueError("the agent was wrong: record the real root cause before signing off")
            cols = ", ".join(self.RCA_FIELDS)
            self._x(f"INSERT INTO rca_notes (incident_id, {cols}, updated_by, updated_at) "
                    f"VALUES ({', '.join('?' * (len(self.RCA_FIELDS) + 3))}) ON CONFLICT(incident_id) DO UPDATE SET "
                    + ", ".join(f"{c}=excluded.{c}" for c in (*self.RCA_FIELDS, "updated_by", "updated_at")),
                    (incident_id, *values.values(), user, iso()))
            self.audit(user, "rca.updated", incident_id, {k: bool(v) for k, v in values.items()})
            if sign_off:
                self._x("UPDATE rca_notes SET signed_off_by=?, signed_off_at=? WHERE incident_id=?",
                        (user, iso(), incident_id))
                self.audit(user, "rca.signed_off", incident_id, {"agent_verdict": values["agent_verdict"]})
            return self.rca_notes(incident_id)

    # ------------------------------------------------------------------ learning from past incidents

    def history_candidates(self, incident_type: str, namespace: str | None, exclude: str,
                           limit: int = 300) -> list[dict[str, Any]]:
        """Earlier investigated incidents of the same type or in the same namespace, with their RCA notes."""
        rows = self._q(
            "SELECT i.*, n.actions_taken, n.root_cause_confirmation, n.agent_verdict, n.actual_cause_type, "
            "n.signed_off_by, n.signed_off_at FROM incidents i LEFT JOIN rca_notes n ON n.incident_id = i.id "
            "WHERE i.id != ? AND i.report_json IS NOT NULL AND (i.incident_type = ? OR i.namespace IS ?) "
            "ORDER BY i.seq DESC LIMIT ?", (exclude, incident_type, namespace, limit))
        out = []
        for r in rows:
            d = _incident(r)
            for k in ("actions_taken", "root_cause_confirmation", "agent_verdict", "actual_cause_type",
                      "signed_off_by", "signed_off_at"):
                d[k] = r[k]
            out.append(d)
        return out

    def accuracy(self) -> dict[str, Any]:
        """How often engineers confirmed the agent's diagnosis, per incident type (signed-off RCAs only)."""
        rows = self._q("SELECT i.incident_type, i.report_json, n.agent_verdict FROM rca_notes n "
                       "JOIN incidents i ON i.id = n.incident_id WHERE n.signed_off_by IS NOT NULL "
                       "AND n.agent_verdict IS NOT NULL AND n.agent_verdict != ''")
        by_type: dict[str, dict[str, Any]] = {}
        conf: dict[str, list[float]] = {v: [] for v in self.VERDICTS}
        for r in rows:
            t = by_type.setdefault(r["incident_type"] or "unknown",
                                   {"correct": 0, "partial": 0, "incorrect": 0, "reviewed": 0})
            t[r["agent_verdict"]] += 1
            t["reviewed"] += 1
            with contextlib.suppress(Exception):
                conf[r["agent_verdict"]].append(float(json.loads(r["report_json"])["root_cause_confidence"]))

        def score(t: dict[str, Any]) -> float | None:
            return round((t["correct"] + 0.5 * t["partial"]) / t["reviewed"], 3) if t["reviewed"] else None

        total = {k: sum(t[k] for t in by_type.values()) for k in ("correct", "partial", "incorrect", "reviewed")}
        for t in by_type.values():
            t["accuracy"] = score(t)
        return {
            "overall": {**total, "accuracy": score(total)},
            "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1]["reviewed"])),
            # calibration: a well-calibrated agent is more confident when it is right
            "avg_confidence": {v: (round(sum(xs) / len(xs), 3) if xs else None) for v, xs in conf.items()},
        }

    def audit_done(self, incident_id: str, action: str) -> bool:
        return bool(self._q("SELECT 1 FROM audit_log WHERE incident_id=? AND action=? LIMIT 1", (incident_id, action)))

    # ------------------------------------------------------------------ audit + email dedup

    def audit(self, actor: str, action: str, incident_id: str | None = None, details: dict[str, Any] | None = None) -> None:
        self._x("INSERT INTO audit_log (ts, actor, action, incident_id, details) VALUES (?,?,?,?,?)",
                (iso(), actor, action, incident_id, json.dumps(details or {})))

    def audit_log(self, incident_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if incident_id:
            rows = self._q("SELECT * FROM audit_log WHERE incident_id=? ORDER BY id DESC LIMIT ?", (incident_id, limit))
        else:
            rows = self._q("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def email_recently_sent(self, dedup_key: str, window_minutes: int) -> bool:
        rows = self._q("SELECT sent_at FROM email_dedup WHERE dedup_key=?", (dedup_key,))
        if not rows:
            return False
        return datetime.fromisoformat(rows[0]["sent_at"]) > now() - timedelta(minutes=window_minutes)

    def record_email(self, dedup_key: str, incident_id: str) -> None:
        self._x("INSERT OR REPLACE INTO email_dedup (dedup_key, incident_id, sent_at) VALUES (?,?,?)",
                (dedup_key, incident_id, iso()))


def _incident(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in ("alert_json", "evidence_json", "report_json"):
        d[key.removesuffix("_json")] = json.loads(d.pop(key)) if d.get(key) else None
    d["alertnames"] = json.loads(d["alertnames"])
    return d
