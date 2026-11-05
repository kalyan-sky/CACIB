"""Prometheus metrics about the agent itself (scraped at /metrics)."""

from prometheus_client import Counter, Gauge, Histogram

ALERTS_RECEIVED = Counter("sre_agent_alerts_received_total", "Alerts received from Alertmanager",
                          ["cluster", "status"])
ALERTS_PROCESSED = Counter("sre_agent_alerts_processed_total", "Alerts fully processed",
                           ["cluster", "outcome"])
INVESTIGATION_SECONDS = Histogram("sre_agent_investigation_duration_seconds", "Evidence collection time",
                                  ["cluster"], buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120))
ANALYSIS_SECONDS = Histogram("sre_agent_ai_analysis_duration_seconds", "Root cause analysis time",
                             ["engine"], buckets=(0.01, 0.1, 0.5, 1, 5, 10, 30, 60, 120))
AI_FAILURES = Counter("sre_agent_ai_failures_total", "LLM analysis failures (fell back to rules)", ["provider"])
EMAILS_SENT = Counter("sre_agent_emails_sent_total", "Incident emails sent")
EMAIL_FAILURES = Counter("sre_agent_email_delivery_failures_total", "Incident emails that failed")
INCIDENTS = Counter("sre_agent_incidents_total", "Incidents created", ["cluster", "severity"])
PENDING_APPROVALS = Gauge("sre_agent_pending_approvals", "Approval records waiting for a decision")
K8S_API_ERRORS = Counter("sre_agent_kubernetes_api_errors_total", "Kubernetes API errors",
                         ["cluster", "operation", "status"])
INVESTIGATION_TIMEOUTS = Counter("sre_agent_investigation_timeouts_total", "Investigations that hit the time limit",
                                 ["cluster"])
QUEUE_DEPTH = Gauge("sre_agent_queue_depth", "Alerts waiting to be investigated")
