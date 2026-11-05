"""Read-only Kubernetes SRE agent.

Receives Alertmanager alerts, investigates them with read-only Kubernetes API
calls, explains the probable root cause, and proposes a resolution for a human
to approve and carry out. It never changes anything in a cluster.
"""

__version__ = "0.1.0"
