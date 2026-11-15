"""Runbooks for alerts that no evidence checker explains (cluster-wide, platform, business alerts).

A runbook never claims a root cause. It gives the likely causes as hypotheses
and the read-only commands that tell them apart, so every alert becomes a
handled incident with a clear next step, even without pod or node evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Runbook:
    category: str
    meaning: str
    likely_causes: list[str] = field(default_factory=list)
    checks: list[tuple[str, str]] = field(default_factory=list)  # (read-only command, what it shows)


PROM = "kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090  # then open /alerts and /targets"

RUNBOOKS: dict[str, Runbook] = {
    # ---------------------------------------------------------------- monitoring stack
    "TargetDown": Runbook(
        "MonitoringTargetDown", "Prometheus cannot scrape one or more targets of a job, so its metrics and alerts are blind.",
        ["The pods behind the job are down or restarting", "A NetworkPolicy blocks Prometheus from the namespace",
         "The ServiceMonitor port/path no longer matches the Service"],
        [(PROM, "Status > Targets shows the failing endpoints and the scrape error"),
         ("kubectl get servicemonitors,podmonitors -A", "Which monitor defines the job")]),
    "PrometheusRuleFailures": Runbook(
        "MonitoringRuleFailure", "Prometheus fails to evaluate rules, so some alerts can never fire.",
        ["A rule references a metric or label that changed", "Query too expensive (timeouts)"],
        [(PROM, "Status > Rules shows the failing rule and its error")]),
    "PrometheusNotConnectedToAlertmanagers": Runbook(
        "MonitoringAlertDelivery", "Alerts are evaluated but cannot reach Alertmanager: nobody is notified.",
        ["Alertmanager pods down", "Service discovery or network problem"],
        [("kubectl -n monitoring get pods -l app.kubernetes.io/name=alertmanager", "Alertmanager health")]),
    "AlertmanagerFailedToSendAlerts": Runbook(
        "MonitoringAlertDelivery", "Alertmanager cannot deliver notifications to a receiver (e.g. the SRE agent).",
        ["The receiver is down", "Webhook token mismatch (401)", "NetworkPolicy blocks the receiver"],
        [("kubectl -n monitoring logs statefulset/alertmanager-monitoring-kube-prometheus-alertmanager -c alertmanager --tail=50",
          "The delivery error per receiver")]),
    "GrafanaDown": Runbook(
        "ObservabilityDegraded", "Grafana is not answering: dashboards are unavailable (alerting still works).",
        ["Grafana pod OOMKilled by a heavy query", "Database locked (SQLite) or volume problem", "Node pressure"],
        [("kubectl -n monitoring get pods -l app.kubernetes.io/name=grafana", "Restarts / OOMKilled"),
         ("kubectl -n monitoring logs deploy/monitoring-grafana -c grafana --tail=100", "database is locked, timeouts")]),
    "SreAgentDown": Runbook(
        "AgentDown", "The SRE agent is not running: alerts are not being investigated.",
        ["Pod crash (see logs)", "Volume not attached", "Image pull failure"],
        [("kubectl -n sre-agent get pods,pvc", "Pod and volume state"),
         ("kubectl -n sre-agent logs deploy/sre-agent --tail=50", "Start-up errors")]),
    "SreAgentEmailFailing": Runbook(
        "AgentEmailFailing", "Incident emails are not being delivered.",
        ["Gmail App Password revoked or changed", "SMTP egress blocked (port 587)"],
        [("kubectl -n sre-agent logs deploy/sre-agent | grep 'email failed' | tail -5", "The SMTP error")]),
    # ---------------------------------------------------------------- workloads
    "KubeJobFailed": Runbook(
        "JobFailed", "A Job ran out of retries.",
        ["The batch code failed (see the pod logs)", "A dependency was unavailable", "Deadline exceeded"],
        [("kubectl -n <namespace> get jobs,pods", "Failed pods of the job"),
         ("kubectl -n <namespace> logs job/<job> --tail=100", "Why the last attempt failed")]),
    "KubeJobNotCompleted": Runbook(
        "JobSlow", "A Job is taking longer than expected.",
        ["Larger input than usual", "Throttled CPU or waiting on a dependency"],
        [("kubectl -n <namespace> describe job <job>", "Start time, active pods, events")]),
    "KubeStatefulSetReplicasMismatch": Runbook(
        "StatefulSetDegraded", "A StatefulSet has fewer ready replicas than desired.",
        ["A pod cannot start (volume, scheduling, crash)", "Volume in another AZ than the free capacity"],
        [("kubectl -n <namespace> get pods,pvc -o wide", "Which replica is missing and why")]),
    "KubeDaemonSetRolloutStuck": Runbook(
        "DaemonSetDegraded", "A DaemonSet is not running on every node it should.",
        ["A node with insufficient resources or a taint", "Image or config problem on the new version"],
        [("kubectl -n <namespace> get ds -o wide; kubectl -n <namespace> get pods -o wide", "Nodes without a ready pod")]),
    "KubeHpaMaxedOut": Runbook(
        "AutoscalingLimit", "A HorizontalPodAutoscaler is at its maximum replicas: more load cannot be absorbed.",
        ["Real traffic growth", "Inefficient code or a slow dependency raising CPU per request"],
        [("kubectl -n <namespace> describe hpa", "Current metrics versus targets")]),
    "KubeContainerWaiting": Runbook(
        "ContainerWaiting", "A container has been waiting to start for over an hour.",
        ["Image pull, config reference or volume problem (see the waiting reason)"],
        [("kubectl -n <namespace> describe pod <pod>", "The waiting reason and events")]),
    "PodStuckTerminating": Runbook(
        "PodStuckTerminating", "A pod has been terminating for over 10 minutes.",
        ["A finalizer that never completes", "The node is gone or NotReady", "A volume that cannot detach"],
        [("kubectl -n <namespace> get pod <pod> -o jsonpath='{.metadata.finalizers}'", "Finalizers holding it"),
         ("kubectl get nodes", "Is its node Ready?")]),
    "ContainerMemoryNearLimit": Runbook(
        "MemoryNearLimit", "A container uses over 90% of its memory limit: an OOM kill is likely soon.",
        ["Limit set below the real working set", "A leak growing over time"],
        [("kubectl -n <namespace> top pod <pod> --containers", "Usage now versus the limit"),
         ("Grafana: Kubernetes / Compute Resources / Pod, last 24 h", "Steady (undersized) or growing (leak)?")]),
    "ExcessiveCPURequests": Runbook(
        "OverProvisioned", "A pod requests over 10x the CPU it uses (FinOps finding, not an outage).",
        ["Requests copied from a sizing sheet instead of measured"],
        [("kubectl -n <namespace> top pod <pod>", "Actual usage")]),
    # ---------------------------------------------------------------- capacity and nodes
    "KubeQuotaAlmostFull": Runbook(
        "QuotaNearlyFull", "A namespace ResourceQuota is nearly used up: the next rollout may fail.",
        ["Growth or a new workload", "Oversized requests"],
        [("kubectl -n <namespace> describe resourcequota", "Used versus hard")]),
    "KubeQuotaFullyUsed": Runbook(
        "QuotaFull", "A namespace ResourceQuota is fully used: new pods (including rolling updates) are rejected.",
        ["Growth or a new workload", "Oversized requests"],
        [("kubectl -n <namespace> describe resourcequota", "Used versus hard")]),
    "KubeCPUOvercommit": Runbook(
        "ClusterCapacity", "Requested CPU exceeds what the cluster could absorb if a node fails.",
        ["Growth without adding nodes", "Oversized requests"],
        [("kubectl describe nodes | grep -A8 'Allocated resources'", "Requests per node")]),
    "KubeMemoryOvercommit": Runbook(
        "ClusterCapacity", "Requested memory exceeds what the cluster could absorb if a node fails.",
        ["Growth without adding nodes", "Oversized requests"],
        [("kubectl describe nodes | grep -A8 'Allocated resources'", "Requests per node")]),
    "NodeFilesystemSpaceFillingUp": Runbook(
        "NodeDiskFilling", "A node's disk is predicted to fill up.",
        ["Container logs or images accumulating", "emptyDir volumes without size limits"],
        [("kubectl describe node <node>", "DiskPressure condition, image GC events")]),
    "NodeFilesystemAlmostOutOfSpace": Runbook(
        "NodeDiskFilling", "A node's disk is almost full; evictions follow.",
        ["Container logs or images accumulating", "emptyDir volumes without size limits"],
        [("kubectl describe node <node>", "DiskPressure condition, image GC events")]),
    "NodeHighCPU": Runbook(
        "NodeHighCPU", "A node's CPU has been above 90% for 15 minutes.",
        ["Pods without CPU limits", "Too many pods on one node (no spread)"],
        [("kubectl top pods -A --sort-by=cpu | head -15", "Heaviest pods"),
         ("kubectl get pods -A -o wide --field-selector spec.nodeName=<node>", "What runs there")]),
    "NodeHighMemory": Runbook(
        "NodeHighMemory", "A node's memory has been above 90% for 15 minutes; MemoryPressure and evictions may follow.",
        ["Pods without memory limits", "Memory requests far below real usage"],
        [("kubectl top pods -A --sort-by=memory | head -15", "Heaviest pods")]),
    "NodeClockNotSynchronising": Runbook(
        "ClockSkew", "A node's clock is not synchronised; TLS and token validation can fail.",
        ["NTP (chrony) not running or blocked"],
        [("aws ssm start-session --target <instance>  # then: chronyc tracking", "Clock offset")]),
    "KubeClientCertificateExpiration": Runbook(
        "CertificateExpiring", "A client certificate used against the API server expires soon.",
        ["A kubeconfig or component certificate not rotated"],
        [("kubectl get csr", "Pending certificate signing requests"),
         ("openssl x509 -noout -enddate -in <cert>", "Exact expiry")]),
    # ---------------------------------------------------------------- persistence
    "KubePersistentVolumeFillingUp": Runbook(
        "VolumeFilling", "A PersistentVolume is filling up.",
        ["Data growth or missing retention/cleanup"],
        [("kubectl -n <namespace> get pvc", "Size per claim"),
         ("kubectl get storageclass -o custom-columns=NAME:.metadata.name,EXPAND:.allowVolumeExpansion", "Can it be expanded?")]),
    "KubePersistentVolumeErrors": Runbook(
        "VolumeError", "A PersistentVolume is in a Failed or Pending state.",
        ["The backing EBS volume was deleted or is in another AZ", "CSI driver problem"],
        [("kubectl get pv", "Phase per volume"), ("kubectl -n kube-system logs deploy/ebs-csi-controller -c ebs-plugin --tail=50",
                                                  "CSI errors")]),
    "PersistentVolumeFailed": Runbook(
        "VolumeError", "A PersistentVolume is Failed.",
        ["The backing EBS volume was deleted", "Reclaim failed"],
        [("kubectl describe pv <pv>", "Events and the backing volume ID")]),
    # ---------------------------------------------------------------- DNS and networking
    "CoreDNSErrorsHigh": Runbook(
        "DNSErrors", "CoreDNS answers many queries with SERVFAIL.",
        ["Upstream (VPC) resolver problems", "CoreDNS overloaded (too few replicas)", "Misconfigured forward zone"],
        [("kubectl -n kube-system logs deploy/coredns --tail=100", "Upstream errors"),
         ("kubectl -n kube-system get deploy coredns", "Replicas")]),
    "AwsCniIpPoolLow": Runbook(
        "IpExhaustion", "The VPC CNI has assigned over 90% of its pod IP addresses: new pods may get no IP.",
        ["Subnets too small for the pod count", "Prefix delegation off", "Warm IP settings too generous"],
        [("kubectl -n kube-system logs ds/aws-node -c aws-node --tail=50 | grep -i 'ipam\\|no available'", "IPAM errors"),
         ("aws ec2 describe-subnets --filters Name=vpc-id,Values=<vpc> --query 'Subnets[].[SubnetId,AvailableIpAddressCount]'",
          "Free IPs per subnet")]),
    # ---------------------------------------------------------------- delivery (Argo CD)
    "ArgoAppDegraded": Runbook(
        "DeploymentDegraded", "An Argo CD application is Degraded or Missing: what runs differs from what Git says, unhealthily.",
        ["A rollout failing (image, config, probes)", "A resource deleted outside Git"],
        [("kubectl -n argocd get applications", "Sync and health per application"),
         ("Argo CD UI > application > the red resources", "Which object is unhealthy")]),
    "ArgoAppSyncFailed": Runbook(
        "DeploymentSyncFailed", "Argo CD failed to sync an application: the latest Git change is not deployed.",
        ["A manifest the AppProject does not permit", "Invalid manifest", "Admission webhook rejection"],
        [("kubectl -n argocd get application <app> -o jsonpath='{.status.operationState.message}'", "The sync error")]),
    "ArgoAppOutOfSync": Runbook(
        "ConfigurationDrift", "The cluster differs from Git (drift) for over 30 minutes.",
        ["A manual kubectl change", "selfHeal off and a sync not run"],
        [("Argo CD UI > App Diff", "Exactly what differs")]),
    # ---------------------------------------------------------------- business flows
    "BankAppHighFailureRate": Runbook(
        "BusinessFlowFailing", "A banking service fails over 5% of its transactions.",
        ["One of its upstream dependencies is failing or blocked", "A bad release", "Configuration (FAIL_RATE) change"],
        [("Grafana: Bank - transactions overview > Upstream errors", "Which dependency fails"),
         ("kubectl -n <namespace> rollout history deployment/<app>", "Recent releases")]),
    "BankAppSlow": Runbook(
        "BusinessFlowSlow", "A banking service averages over 1 s per transaction.",
        ["A slow upstream", "CPU throttling", "Configured latency change"],
        [("Grafana: Bank - transactions overview > Slowest apps", "Where the time goes")]),
    "BankAppDown": Runbook(
        "BusinessServiceDown", "A banking service pod does not answer /metrics.",
        ["The pod is crashing or not ready", "A NetworkPolicy change blocks monitoring"],
        [("kubectl -n <namespace> get pods -o wide", "Pod state")]),
}


def lookup(alertname: str) -> Runbook | None:
    if alertname in RUNBOOKS:
        return RUNBOOKS[alertname]
    for prefix, key in (("KubeQuota", "KubeQuotaFullyUsed"), ("NodeFilesystem", "NodeFilesystemSpaceFillingUp"),
                        ("Prometheus", "PrometheusRuleFailures"), ("Alertmanager", "AlertmanagerFailedToSendAlerts")):
        if alertname.startswith(prefix):
            return RUNBOOKS[key]
    return None
