"""Deterministic root cause analysis.

Each checker looks at the collected evidence for one failure pattern. A checker
only claims a root cause when an observed fact supports it (an event message, a
termination reason, a log line, a node condition), and it cites that fact as
evidence. Anything it infers beyond the facts goes into the contributing
factors as a hypothesis. If no checker matches, the result is inconclusive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..models import (INCONCLUSIVE, Command, Evidence, IncidentReport, InvestigationEvidence, NormalizedAlert,
                      ResolutionStep, severity_rank)


@dataclass
class Finding:
    incident_type: str
    summary: str
    root_cause: str
    status: str  # confirmed | probable | inconclusive
    confidence: float
    evidence: list[Evidence]
    factors: list[str] = field(default_factory=list)
    investigations: list[Command] = field(default_factory=list)
    remediation: list[Command] = field(default_factory=list)
    verification: list[Command] = field(default_factory=list)
    rollback: list[Command] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    expected_outcome: str = ""
    risk: str = ""
    severity_floor: str | None = None  # e.g. CoreDNS down is never below CRITICAL


def D(cmd: str, why: str, expect: str = "") -> Command:
    return Command(kind="diagnostic", command=cmd, explanation=why, expected_output=expect)


def R(cmd: str, why: str, expect: str = "") -> Command:
    return Command(kind="remediation", command=cmd, explanation=why, expected_output=expect)


def V(cmd: str, why: str, expect: str = "") -> Command:
    return Command(kind="verification", command=cmd, explanation=why, expected_output=expect)


def B(cmd: str, why: str, expect: str = "") -> Command:
    return Command(kind="rollback", command=cmd, explanation=why, expected_output=expect)


EXIT_CODES = {
    0: "exited normally (a long-running container must not exit)",
    1: "application error",
    2: "misuse of a shell built-in / bad arguments",
    126: "command found but not executable",
    127: "command not found in the image",
    128: "invalid exit argument",
    134: "aborted (SIGABRT)",
    137: "killed with SIGKILL (OOM kill or a failed liveness probe)",
    139: "segmentation fault (SIGSEGV)",
    143: "terminated with SIGTERM",
}

LOG_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\[emerg\]|configuration file .* test failed|unknown directive|unexpected \"?[}{;]", re.I),
     "its configuration file is invalid, so the process refuses to start", "config"),
    (re.compile(r"KeyError|is not set|missing required (config|env|setting)|required (config|environment variable)"
                r"|environment variable .* (missing|not set|required)", re.I),
     "a required configuration value or environment variable is missing", "config"),
    (re.compile(r"(yaml|json|toml|ini).{0,40}(error|invalid|unexpected)|ParseError|syntax error", re.I),
     "a configuration file cannot be parsed", "config"),
    (re.compile(r"exec format error", re.I), "the image was built for a different CPU architecture", "image"),
    (re.compile(r"connection refused|ECONNREFUSED|could not connect|no route to host|dial tcp .* (timeout|i/o)", re.I),
     "a dependency it connects to at start-up is unreachable", "dependency"),
    (re.compile(r"permission denied|EACCES|operation not permitted", re.I),
     "it lacks permission to a file, port or directory it needs", "permissions"),
    (re.compile(r"no such file or directory|command not found|executable file not found", re.I),
     "a file or command it needs does not exist in the container", "image"),
    (re.compile(r"out of memory|MemoryError|Cannot allocate memory|heap out of memory|OutOfMemoryError", re.I),
     "it runs out of memory", "memory"),
    (re.compile(r"panic:|Traceback \(most recent call last\)|Unhandled exception|FATAL|fatal error", re.I),
     "the application crashes with an unhandled error", "app"),
]

GITOPS_NOTE = ("This workload is deployed by Argo CD (GitOps). Make the change in Git and let Argo CD sync it; "
               "a kubectl change is drift and is overwritten or flagged at the next sync.")


class Ctx:
    """Convenient access to the evidence for one incident."""

    def __init__(self, alert: NormalizedAlert, ev: InvestigationEvidence):
        self.alert, self.ev = alert, ev
        self.ns = alert.namespace or ev.namespace or "<namespace>"
        self.pod = ev.pods[0] if ev.pods else None
        self.dep = ev.deployment
        self.dep_name = (self.dep or {}).get("name") or alert.deployment or alert.workload or "<deployment>"
        self.pod_name = (self.pod or {}).get("name") or alert.pod or "<pod>"
        self.gitops = bool((self.dep or {}).get("managed_by_gitops"))

    def containers(self) -> list[dict[str, Any]]:
        return [c for p in self.ev.pods for c in p.get("containers", [])]

    def events(self, *reasons: str, contains: str | None = None) -> list[dict[str, Any]]:
        out = []
        for e in self.ev.events:
            if reasons and e.get("reason") not in reasons:
                continue
            if contains and contains.lower() not in (e.get("message") or "").lower():
                continue
            out.append(e)
        return out

    def ev_event(self, e: dict[str, Any]) -> Evidence:
        n = f" (x{e['count']})" if (e.get("count") or 1) > 1 else ""
        return Evidence(source="events", observation=f"{e['object']}: {e['reason']}: {e['message']}{n}")

    def describe_pod(self) -> Command:
        return D(f"kubectl -n {self.ns} describe pod {self.pod_name}",
                 "Container states, restart counts, resource settings and the pod's events (at the bottom).",
                 "The Events section shows the same message as this report.")

    def get_pods(self) -> Command:
        sel = ",".join(f"{k}={v}" for k, v in ((self.dep or {}).get("selector") or {}).items())
        return D(f"kubectl -n {self.ns} get pods -o wide" + (f" -l {sel}" if sel else ""),
                 "Current status, restarts and node of the affected pods.")

    def verify_rollout(self) -> list[Command]:
        return [V(f"kubectl -n {self.ns} rollout status deployment/{self.dep_name} --timeout=180s",
                  "Waits until every replica of the new version is available.",
                  f'deployment "{self.dep_name}" successfully rolled out'),
                V(f"kubectl -n {self.ns} get pods -o wide -l {self.selector()}" if self.selector() else
                  f"kubectl -n {self.ns} get pods -o wide",
                  "Pods should be Running and READY with no new restarts.")]

    def selector(self) -> str:
        return ",".join(f"{k}={v}" for k, v in ((self.dep or {}).get("selector") or {}).items())

    def undo(self) -> Command:
        return B(f"kubectl -n {self.ns} rollout undo deployment/{self.dep_name}",
                 "Returns the Deployment to its previous revision if the change makes things worse"
                 + (" (with GitOps: revert the commit instead)." if self.gitops else "."),
                 f"deployment.apps/{self.dep_name} rolled back")


# ---------------------------------------------------------------------- checkers

def check_config_error(c: Ctx) -> Finding | None:
    for ct in c.containers():
        if ct["state"] != "waiting:CreateContainerConfigError":
            continue
        msg = ct.get("state_message") or ""
        evs = c.events("Failed", contains="Error:")
        evidence = [Evidence(source="pod.status", observation=f"container {ct['name']} is waiting: "
                                                              f"CreateContainerConfigError: {msg}")]
        evidence += [c.ev_event(e) for e in evs[:2]]
        m = re.search(r"couldn't find key (\S+) in (Secret|ConfigMap) ([\w.-]+)/([\w.-]+)", msg)
        m2 = re.search(r'(secret|configmap) "([\w.-]+)" not found', msg, re.I)
        if m:
            key, kind, _, obj = m.groups()
            refs = [r for r in ct.get("env_refs", []) if key in str(r)]
            if refs:
                evidence.append(Evidence(source="pod.spec", observation=f"env {refs[0]['name']} reads key '{key}' "
                                                                        f"from {kind} {obj}"))
            cause = (f"The container reads key '{key}' from {kind} '{obj}', but that {kind} has no such key, "
                     "so the kubelet cannot build the container's environment.")
            fix_obj = f"{kind.lower()} {obj}"
            remediation = [
                D(f"kubectl -n {c.ns} describe {fix_obj}",
                  f"Lists the key names in the {kind} (values are not shown).",
                  f"Look for a key that is close to '{key}' (a typo) or missing."),
                R(f"kubectl -n {c.ns} patch {fix_obj} --type merge -p "
                  f"'{{\"stringData\":{{\"{key}\":\"<value>\"}}}}'" if kind == "Secret" else
                  f"kubectl -n {c.ns} patch {fix_obj} --type merge -p '{{\"data\":{{\"{key}\":\"<value>\"}}}}'",
                  f"Adds the missing key. Prefer fixing the {kind} over renaming the reference: other consumers "
                  f"may depend on the key name '{key}'. Never paste a real secret value into a ticket or chat.",
                  f"{kind.lower()}/{obj} patched"),
                R(f"kubectl -n {c.ns} delete pod {c.pod_name}",
                  "Environment variables are read when a container starts; a new pod picks up the key.",
                  f'pod "{c.pod_name}" deleted (its ReplicaSet creates a replacement)'),
            ]
        elif m2:
            kind, obj = m2.groups()
            cause = f"The pod references {kind} '{obj}', which does not exist in namespace {c.ns}."
            remediation = [
                D(f"kubectl -n {c.ns} get {kind.lower()}s", f"Lists the {kind}s that exist; check for a typo."),
                R(f"kubectl -n {c.ns} create {kind.lower()} generic {obj} --from-literal=<key>=<value>"
                  if kind.lower() == "secret" else f"kubectl -n {c.ns} create configmap {obj} --from-literal=<key>=<value>",
                  f"Creates the missing {kind} (or correct the reference in the Deployment if the name is wrong).")]
        else:
            cause = f"The kubelet cannot build the container's configuration: {msg or 'see the events'}."
            remediation = []
        return Finding(
            "CreateContainerConfigError", f"{c.pod_name} cannot start: its configuration references are invalid.",
            cause, "confirmed" if (m or m2) else "probable", 0.95 if (m or m2) else 0.6, evidence,
            factors=["A typo in a Secret/ConfigMap key or name introduced by the last change (hypothesis)."],
            investigations=[c.describe_pod()],
            remediation=remediation,
            verification=[V(f"kubectl -n {c.ns} get pods -o wide", "The new pod should be Running 1/1."),
                          V(f"kubectl -n {c.ns} get events --field-selector involvedObject.name={c.pod_name}",
                            "No more CreateContainerConfigError events.")],
            rollback=[B("Restore the previous version of the Secret/ConfigMap from Git or your secret store",
                        "If the new value breaks the application, put the previous one back.")],
            prerequisites=[f"Write access to {c.ns} (or a merge request if the object is managed in Git)",
                           "The correct value from the secret owner (never guess credentials)"],
            expected_outcome="The pod starts and becomes Ready.",
            risk="Low: adding a missing key does not affect running pods. Wrong values could break the app's "
                 "connection to its dependency.")
    return None


def check_image_pull(c: Ctx) -> Finding | None:
    bad = [ct for ct in c.containers() if ct.get("image_pull_failed")]
    if not bad:
        return None
    ct = bad[0]
    image = ct.get("image") or "<image>"
    evs = c.events("Failed", "BackOff", "ErrImagePull", contains=None)
    pull_msgs = [e for e in evs if "pull" in (e.get("message") or "").lower() or "image" in (e.get("message") or "").lower()]
    text = " ".join(e.get("message") or "" for e in pull_msgs).lower() + " " + (ct.get("state_message") or "").lower()
    evidence = [Evidence(source="pod.status", observation=f"container {ct['name']} is {ct['state']} for image {image}")]
    evidence += [c.ev_event(e) for e in pull_msgs[:2]]
    if re.search(r"not found|manifest unknown|does not exist|name unknown", text):
        cause = f"The image reference '{image}' does not exist in the registry (wrong tag or repository name)."
        conf, status = 0.9, "confirmed"
    elif re.search(r"unauthorized|denied|403|401|authentication required|pull access denied|no basic auth", text):
        cause = (f"The registry refused to serve '{image}': the node or pod has no credentials for it "
                 "(private repository, missing imagePullSecret, or missing ECR read permission on the node role).")
        conf, status = 0.85, "confirmed"
    elif re.search(r"no such host|i/o timeout|dial tcp|tls handshake timeout|connection reset", text):
        cause = (f"The node cannot reach the registry for '{image}' (DNS, NAT/egress or firewall problem).")
        conf, status = 0.75, "probable"
    elif "invalidimagename" in ct["state"].lower():
        cause = f"The image reference '{image}' is not a valid image name."
        conf, status = 0.9, "confirmed"
    else:
        cause = f"The kubelet cannot pull '{image}'; the event message does not say why."
        conf, status = 0.5, "probable"
    repo, _, tag = image.rpartition(":") if ":" in image.split("/")[-1] else (image, "", "latest")
    return Finding(
        "ImagePullBackOff", f"{c.pod_name} cannot start: the image '{image}' cannot be pulled.", cause, status, conf,
        evidence,
        factors=["A typo in the image tag in the last change (hypothesis)",
                 "The tag was never pushed because the build failed (hypothesis)"],
        investigations=[c.describe_pod(),
                        D(f"crane ls {repo}  # or: aws ecr describe-images --repository-name <repo>",
                          "Lists the tags that exist in the registry (run from a machine with registry access).",
                          f"Check whether '{tag}' is in the list.")],
        remediation=[R(f"kubectl -n {c.ns} set image deployment/{c.dep_name} {ct['name']}={repo}:<correct-tag>",
                       "Points the Deployment at an image tag that exists. " + (GITOPS_NOTE if c.gitops else ""),
                       f"deployment.apps/{c.dep_name} image updated")],
        verification=c.verify_rollout(),
        rollback=[c.undo()],
        prerequisites=["The correct image tag (from the release notes or the CI pipeline)"],
        expected_outcome="New pods pull the image and become Ready; with maxUnavailable 0 the old pods kept serving.",
        risk="Low for a tag correction. If old pods were replaced already, the service may be degraded until fixed.")


def check_oom(c: Ctx) -> Finding | None:
    for p in c.ev.pods:
        for ct in p.get("containers", []):
            last = ct.get("last_terminated") or {}
            if last.get("reason") != "OOMKilled" and ct["state"] != "terminated:OOMKilled":
                continue
            limit = ((ct.get("resources") or {}).get("limits") or {}).get("memory", "not set")
            request = ((ct.get("resources") or {}).get("requests") or {}).get("memory", "not set")
            usage = ((c.ev.metrics.get("pods") or {}).get(p["name"]) or {}).get(ct["name"], {}).get("memory")
            evidence = [Evidence(source="pod.status",
                                 observation=f"container {ct['name']} last terminated: OOMKilled, exit code "
                                             f"{last.get('exit_code', 137)}, restarts {ct.get('restart_count', 0)}"),
                        Evidence(source="pod.spec", observation=f"memory request {request}, limit {limit}")]
            if usage:
                evidence.append(Evidence(source="metrics", observation=f"current memory usage {usage}"))
            logs = c.ev.logs.get(f"{p['name']}/{ct['name']}/previous", "")
            last_line = next((ln for ln in reversed(logs.splitlines()) if ln.strip()), "")
            if last_line:
                evidence.append(Evidence(source="logs.previous", observation=f"last line before the kill: {last_line[:300]}"))
            return Finding(
                "OOMKilled", f"{ct['name']} in {p['name']} is killed for exceeding its memory limit ({limit}).",
                f"The container uses more memory than its limit of {limit}, so the kernel's OOM killer ends it "
                "(exit code 137) and Kubernetes restarts it.",
                "confirmed", 0.9, evidence,
                factors=["The limit is too low for the real working set, e.g. copied from a sizing sheet (hypothesis)",
                         "A memory leak after a recent release: usage grows until the limit (hypothesis)",
                         "A traffic spike or a larger input (cache, batch) than usual (hypothesis)"],
                investigations=[c.describe_pod(),
                                D(f"kubectl -n {c.ns} logs {p['name']} -c {ct['name']} --previous --tail=50",
                                  "What the app did just before it was killed."),
                                D(f"kubectl -n {c.ns} top pod {p['name']} --containers",
                                  "Memory usage now; compare with the limit."),
                                D(f"kubectl -n {c.ns} rollout history deployment/{c.dep_name}",
                                  "Did a recent release change memory behaviour?")],
                remediation=[R(f"kubectl -n {c.ns} set resources deployment/{c.dep_name} -c {ct['name']} "
                               f"--requests=memory=<measured need> --limits=memory=<need + 25-50% headroom>",
                               "Sizes memory from measured usage. If usage grows without bound, raise the limit "
                               "only as a stop-gap and fix the leak (or roll back the release). "
                               + (GITOPS_NOTE if c.gitops else ""))],
                verification=c.verify_rollout() + [V(f"kubectl -n {c.ns} get pod -l {c.selector()} "
                                                     "-o custom-columns=POD:.metadata.name,RESTARTS:.status.containerStatuses[0].restartCount",
                                                     "Restart counts stay the same for 15+ minutes.")],
                rollback=[c.undo()],
                prerequisites=["Memory usage history (Grafana: Kubernetes / Compute Resources / Pod)",
                               "Free capacity in the namespace ResourceQuota for a higher request"],
                expected_outcome="No more OOMKilled restarts; memory stays below the new limit.",
                risk="Medium: a higher limit uses more node memory and quota; a leak will still exhaust it later.")
    return None


def check_liveness(c: Ctx) -> Finding | None:
    fails = c.events("Unhealthy", contains="Liveness probe failed")
    kills = c.events("Killing", contains="liveness")
    if not fails or not (kills or any(ct.get("restart_count", 0) > 0 for ct in c.containers())):
        return None
    ct = next((x for x in c.containers() if x.get("liveness_probe")), c.containers()[0] if c.containers() else {})
    probe = ct.get("liveness_probe") or {}
    startup = ct.get("startup_probe")
    evidence = [c.ev_event(fails[-1])] + [c.ev_event(k) for k in kills[-1:]]
    evidence.append(Evidence(source="pod.spec", observation=f"liveness probe {probe}; startup probe: "
                                                            f"{startup or 'none'}"))
    delay = probe.get("initialDelaySeconds", 0)
    return Finding(
        "LivenessProbeFailure", f"{c.pod_name} is restarted by its liveness probe.",
        f"The liveness probe fails and the kubelet kills the container (initialDelaySeconds={delay}, "
        f"failureThreshold={probe.get('failureThreshold', 3)}). "
        + ("There is no startup probe, so a slow start counts as a failure." if not startup else ""),
        "probable", 0.75, evidence,
        factors=["The application needs longer to start than the probe allows (hypothesis; check the logs for "
                 "start-up time)", "The probe path or port is wrong (hypothesis)",
                 "The app hangs under load and genuinely stops answering (hypothesis)"],
        investigations=[c.describe_pod(),
                        D(f"kubectl -n {c.ns} logs {c.pod_name} --previous --timestamps --tail=50",
                          "Compare the time the app reports 'ready' with the probe's initial delay.")],
        remediation=[R(f"kubectl -n {c.ns} patch deployment {c.dep_name} --type json -p "
                       "'[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/startupProbe\",\"value\":"
                       "{\"httpGet\":" + f"{{\"path\":\"{(probe.get('httpGet') or {}).get('path', '/healthz')}\","
                       f"\"port\":\"{(probe.get('httpGet') or {}).get('port', 8080)}\"}}"
                       ",\"periodSeconds\":5,\"failureThreshold\":30}}]'",
                       "Adds a startup probe so liveness only starts once the app has started (up to 150 s here). "
                       "Do not just remove the liveness probe. " + (GITOPS_NOTE if c.gitops else ""))],
        verification=c.verify_rollout(),
        rollback=[c.undo()],
        expected_outcome="Pods start, pass the startup probe, and stop restarting.",
        risk="Low. Too generous probes delay detection of a genuinely hung process.")


def check_crashloop(c: Ctx) -> Finding | None:
    for p in c.ev.pods:
        for ct in p.get("containers", []):
            last = ct.get("last_terminated") or {}
            crashing = ct["state"] == "waiting:CrashLoopBackOff" or (ct.get("restart_count", 0) >= 3 and last)
            if not crashing:
                continue
            code = last.get("exit_code") if last else ct.get("exit_code")
            evidence = [Evidence(source="pod.status",
                                 observation=f"container {ct['name']} is {ct['state']}, restarts "
                                             f"{ct.get('restart_count', 0)}, last exit code {code} "
                                             f"({EXIT_CODES.get(code, 'see the application logs')})")]
            logs = c.ev.logs.get(f"{p['name']}/{ct['name']}/previous") or c.ev.logs.get(f"{p['name']}/{ct['name']}", "")
            match = None
            for line in reversed(logs.splitlines()[-80:]):
                for pattern, meaning, category in LOG_PATTERNS:
                    if pattern.search(line):
                        match = (line.strip(), meaning, category)
                        break
                if match:
                    break
            cms = p.get("config_maps") or []
            investigations = [c.describe_pod(),
                              D(f"kubectl -n {c.ns} logs {p['name']} -c {ct['name']} --previous --tail=100",
                                "The output of the crashed container: the error is usually in the last lines."),
                              D(f"kubectl -n {c.ns} rollout history deployment/{c.dep_name}",
                                "Did the crashes start with a new revision?")]
            if match:
                line, meaning, category = match
                evidence.append(Evidence(source="logs.previous", observation=f"log line: {line[:400]}"))
                cause = f"The container exits (code {code}) because {meaning}."
                remediation: list[Command] = []
                factors = []
                if category == "config":
                    if cms:
                        evidence.append(Evidence(source="pod.spec", observation=f"configuration comes from ConfigMap(s): "
                                                                                f"{', '.join(cms)}"))
                        investigations.append(D(f"kubectl -n {c.ns} get configmap {cms[0]} -o yaml",
                                                "Shows the configuration the container loads; compare with the error line."))
                        remediation.append(R(f"kubectl -n {c.ns} edit configmap {cms[0]}",
                                             "Correct the value or syntax named in the error. " +
                                             (GITOPS_NOTE if c.gitops else "")))
                        remediation.append(R(f"kubectl -n {c.ns} rollout restart deployment/{c.dep_name}",
                                             "Mounted ConfigMaps update eventually, but environment variables and "
                                             "files read at start-up need a restart."))
                    factors.append("A recent configuration change (check Git history / Argo CD diff) (hypothesis)")
                elif category == "dependency":
                    remediation.append(R("Restore the dependency first (see its own incident), then let the pod retry",
                                         "Restarting this pod does not help while the dependency is down."))
                    factors.append("The dependency is down, or a NetworkPolicy blocks the connection (hypothesis)")
                elif category == "memory":
                    remediation.append(R(f"kubectl -n {c.ns} set resources deployment/{c.dep_name} -c {ct['name']} "
                                         "--limits=memory=<higher>", "See OOM guidance: size from measured usage."))
                remediation.append(R(f"kubectl -n {c.ns} rollout undo deployment/{c.dep_name}",
                                     "Fastest mitigation if the crashes started with the latest revision "
                                     "(restores service while the fix is prepared)."))
                status, conf = "probable", 0.75
            else:
                cause = (f"The container keeps exiting with code {code} ({EXIT_CODES.get(code, 'unknown meaning')}); "
                         "the collected logs do not show why.")
                status, conf, remediation, factors = "inconclusive", 0.3, [], [
                    "The application may log to a file instead of stdout (hypothesis)",
                    "The crash may happen before logging starts (hypothesis)"]
            return Finding(
                "CrashLoopBackOff", f"{ct['name']} in {p['name']} keeps crashing and restarting.", cause, status, conf,
                evidence, factors=factors, investigations=investigations, remediation=remediation,
                verification=c.verify_rollout(), rollback=[c.undo()],
                expected_outcome="The container stays Running and Ready; the restart count stops increasing.",
                risk="Low for a configuration fix; a rollback is the safest mitigation if the cause is uncertain.")
    return None


def check_readiness(c: Ctx) -> Finding | None:
    fails = c.events("Unhealthy", contains="Readiness probe failed")
    not_ready = [ct for ct in c.containers() if ct["state"] == "running" and not ct.get("ready")]
    if not fails or not not_ready:
        return None
    msg = fails[-1].get("message") or ""
    probe = not_ready[0].get("readiness_probe") or {}
    evidence = [c.ev_event(fails[-1]),
                Evidence(source="pod.spec", observation=f"readiness probe {probe}")]
    zero = [s for s in c.ev.services if s.get("ready_endpoints") == 0]
    if zero:
        evidence.append(Evidence(source="services", observation=f"service {zero[0]['name']} has 0 ready endpoints"))
    if "statuscode: 404" in msg:
        cause = (f"The readiness probe calls {(probe.get('httpGet') or {}).get('path')} and the app answers 404: "
                 "the path does not exist, so the pod never becomes Ready and receives no traffic.")
        conf = 0.85
    elif "connection refused" in msg:
        cause = "Nothing listens on the probe's port: wrong port in the probe, or the app listens elsewhere."
        conf = 0.7
    elif "timeout" in msg.lower() or "deadline exceeded" in msg.lower():
        cause = "The app answers the readiness probe too slowly (overloaded, or the probe timeout is too short)."
        conf = 0.6
    else:
        cause = f"The readiness probe fails: {msg}"
        conf = 0.5
    return Finding(
        "ReadinessProbeFailure", f"{c.pod_name} is Running but not Ready, so it receives no traffic.", cause,
        "probable", conf, evidence,
        factors=["A recent change to the probe or to the app's health endpoint (hypothesis)"],
        investigations=[c.describe_pod(),
                        D(f"kubectl -n {c.ns} get endpoints", "Which Services have no ready endpoints.")],
        remediation=[R(f"kubectl -n {c.ns} edit deployment {c.dep_name}",
                       "Set readinessProbe.httpGet.path/port to the endpoint the app really serves "
                       "(check its documentation or `kubectl port-forward` and curl it). "
                       + (GITOPS_NOTE if c.gitops else ""))],
        verification=c.verify_rollout() + [V(f"kubectl -n {c.ns} get endpoints", "Ready addresses appear again.")],
        rollback=[c.undo()],
        expected_outcome="Pods become 1/1 Ready and the Service has endpoints again.",
        risk="Low; with RollingUpdate and maxUnavailable 0 the old pods keep serving until new ones are Ready.")


def check_pvc(c: Ctx) -> Finding | None:
    pending = [p for p in c.ev.pvcs if p.get("phase") == "Pending"]
    if not pending:
        return None
    pvc = pending[0]
    evs = c.events("ProvisioningFailed", "FailedBinding", "ExternalProvisioning")
    evidence = [Evidence(source="pvc", observation=f"PVC {pvc['name']} is Pending, storageClass "
                                                   f"{pvc.get('storage_class')!r}, requests {pvc.get('requested')}")]
    evidence += [c.ev_event(e) for e in evs[:2]]
    evidence.append(Evidence(source="storageclasses", observation="available: " + (", ".join(c.ev.storage_classes) or "none")))
    sc = pvc.get("storage_class")
    names = [s.split(" ")[0] for s in c.ev.storage_classes]
    if sc and sc not in names:
        cause = f"The PVC asks for StorageClass '{sc}', which does not exist, so no volume can be provisioned."
        status, conf = "confirmed", 0.95
    elif not sc and not any("(default)" in s for s in c.ev.storage_classes):
        cause = "The PVC names no StorageClass and the cluster has no default StorageClass."
        status, conf = "confirmed", 0.9
    elif any("waiting for first consumer" in (e.get("message") or "").lower() for e in c.ev.events):
        return None  # normal for WaitForFirstConsumer: the pod's scheduling problem is the real issue
    else:
        cause = "The volume provisioner has not created a volume; see the PVC events."
        status, conf = "probable", 0.5
    default = next((s.split(" ")[0] for s in c.ev.storage_classes if "(default)" in s), "<storage-class>")
    return Finding(
        "PersistentVolumeClaimPending", f"PVC {pvc['name']} is Pending, so pods that mount it cannot start.", cause,
        status, conf, evidence,
        factors=["The manifest was written for another cluster with different StorageClass names (hypothesis)"],
        investigations=[D(f"kubectl -n {c.ns} describe pvc {pvc['name']}", "The provisioning events."),
                        D("kubectl get storageclass", "Which classes exist and which is the default.")],
        remediation=[R(f"kubectl -n {c.ns} get pvc {pvc['name']} -o yaml > pvc.yaml  # set storageClassName: {default}",
                       "storageClassName is immutable: export the claim and change the class in the file."),
                     R(f"kubectl -n {c.ns} delete pvc {pvc['name']} && kubectl -n {c.ns} apply -f pvc.yaml",
                       "Recreates the claim with an existing class. Safe only while it is Pending (it holds no data).")],
        verification=[V(f"kubectl -n {c.ns} get pvc {pvc['name']}", "STATUS Bound."),
                      V(f"kubectl -n {c.ns} get pods -o wide", "The pod that mounts it starts.")],
        rollback=[B(f"kubectl -n {c.ns} apply -f pvc.yaml  # the original export",
                    "Only relevant before data was written; a Bound volume's data stays with its PV.")],
        prerequisites=["Confirm the claim is Pending (never delete a Bound PVC: it may delete data)"],
        expected_outcome="The PVC becomes Bound and the pod starts.",
        risk="Low while Pending. Deleting a Bound PVC can delete its data (reclaimPolicy Delete).")


def check_pending(c: Ctx) -> Finding | None:
    pend = [p for p in c.ev.pods if p.get("phase") == "Pending" and not p.get("node")]
    if not pend:
        return None
    p = pend[0]
    sched = c.events("FailedScheduling")
    if not sched:
        return None
    msg = sched[-1].get("message") or ""
    evidence = [Evidence(source="pod.status", observation=f"pod {p['name']} is Pending with no node assigned"),
                c.ev_event(sched[-1])]
    ct = (p.get("containers") or [{}])[0]
    req = (ct.get("resources") or {}).get("requests") or {}
    remediation: list[Command]
    factors: list[str] = []
    if "Insufficient cpu" in msg or "Insufficient memory" in msg:
        res = "cpu" if "Insufficient cpu" in msg else "memory"
        alloc = [f"{n['name']}: {n['allocatable'].get(res)}" for n in c.ev.nodes if n.get("allocatable")]
        evidence.append(Evidence(source="pod.spec", observation=f"container {ct.get('name')} requests {res}={req.get(res)}"))
        if alloc:
            evidence.append(Evidence(source="nodes", observation=f"allocatable {res} per node: " + "; ".join(alloc[:5])))
        cause = (f"The pod requests {res}={req.get(res)}, and no node has that much unreserved {res} "
                 "(requests count, not actual usage).")
        factors = ["The request was copied from a VM sizing sheet instead of measured usage (hypothesis)",
                   "Other pods' requests fill the nodes; adding a node or right-sizing them would help (hypothesis)"]
        remediation = [R(f"kubectl -n {c.ns} set resources deployment/{c.dep_name} -c {ct.get('name')} "
                         f"--requests={res}=<measured usage + headroom>",
                         "Right-size the request from measured usage (Grafana / kubectl top). "
                         + (GITOPS_NOTE if c.gitops else "")),
                       R("Scale the node group (EKS console > Compute > node group > Edit) or use Karpenter",
                         "Only if the workload genuinely needs the capacity.")]
        status, conf = "confirmed", 0.9
    elif "exceed the max Fargate configuration" in msg:
        cause = (f"The pod's requests ({req}) are larger than the largest Fargate size (16 vCPU / 120 GB), "
                 "so Fargate can never place it.")
        remediation = [R(f"kubectl -n {c.ns} set resources deployment/{c.dep_name} -c {ct.get('name')} "
                         "--requests=cpu=<measured>,memory=<measured>", "Right-size the requests.")]
        status, conf = "confirmed", 0.9
    elif any(k in msg for k in ("didn't match Pod's node affinity/selector", "untolerated taint", "had taint")):
        sel_hit = "didn't match Pod's node affinity/selector" in msg
        taint_hit = "untolerated taint" in msg or "had taint" in msg
        causes, remediation = [], []
        if sel_hit:
            evidence.append(Evidence(source="pod.spec", observation=f"nodeSelector {p.get('node_selector') or '{}'}, "
                                                                    f"affinity set: {p.get('affinity')}"))
            labels = {n["name"]: n.get("labels") for n in c.ev.nodes[:3]}
            if labels:
                evidence.append(Evidence(source="nodes", observation=f"node labels (sample): {labels}"))
            causes.append(f"its nodeSelector/affinity ({p.get('node_selector') or 'affinity rules'}) matches no "
                          "untainted node")
            remediation.append(R(f"kubectl -n {c.ns} edit deployment {c.dep_name}",
                                 "Correct the nodeSelector to a label the target nodes really have, or label the "
                                 "intended nodes via the node group configuration (labels added by hand are lost on "
                                 "new nodes). " + (GITOPS_NOTE if c.gitops else "")))
        if taint_hit:
            taints = {n["name"]: n.get("taints") for n in c.ev.nodes if n.get("taints")}
            evidence.append(Evidence(source="nodes", observation=f"node taints: {taints or 'see the event'}"))
            evidence.append(Evidence(source="pod.spec", observation=f"pod tolerations: {p.get('tolerations') or 'none'}"))
            causes.append("the nodes it is meant for carry a taint it does not tolerate")
            remediation.append(R(f"kubectl -n {c.ns} edit deployment {c.dep_name}",
                                 "Add a toleration matching the taint (key, value, effect) if this workload belongs "
                                 "on those nodes. Do not remove the taint: it keeps other workloads off dedicated "
                                 "nodes. " + (GITOPS_NOTE if c.gitops else "")))
        cause = "The scheduler finds no node for the pod: " + "; and ".join(causes) + "."
        status, conf = "confirmed", 0.9 if len(causes) == 1 else 0.85
    elif "unbound immediate PersistentVolumeClaims" in msg or "persistentvolumeclaim" in msg.lower():
        cause = "The pod waits for a PersistentVolumeClaim that is not bound."
        remediation = [D(f"kubectl -n {c.ns} get pvc", "Find the Pending claim and its StorageClass.")]
        status, conf = "probable", 0.7
    elif "Too many pods" in msg:
        cause = "Every node has reached its maximum pod count (max-pods / IP addresses for VPC CNI)."
        remediation = [R("Add nodes, or enable prefix delegation in the VPC CNI to raise max pods per node",
                         "More pod slots.")]
        status, conf = "confirmed", 0.85
    else:
        cause = f"The scheduler cannot place the pod: {msg}"
        remediation = []
        status, conf = "probable", 0.5
    return Finding(
        "PodPending", f"{p['name']} cannot be scheduled.", cause, status, conf, evidence, factors=factors,
        investigations=[c.describe_pod(), D("kubectl describe nodes | grep -A8 'Allocated resources'",
                                            "Requested CPU/memory per node versus what each node offers.")],
        remediation=remediation, verification=c.verify_rollout(), rollback=[c.undo()],
        expected_outcome="The scheduler assigns a node and the pod starts.",
        risk="Low for right-sizing; adding nodes costs money; editing selectors can move pods to unintended nodes.")


def check_quota(c: Ctx) -> Finding | None:
    evs = c.events("FailedCreate", contains="exceeded quota")
    if not evs:
        return None
    evidence = [c.ev_event(evs[-1])]
    for q in c.ev.quotas[:1]:
        evidence.append(Evidence(source="resourcequota", observation=f"{q['name']}: used {q['used']} / hard {q['hard']}"))
    return Finding(
        "ResourceQuotaExceeded", f"{c.dep_name} cannot create pods: the namespace quota is full.",
        "The ReplicaSet's pod creation is rejected by the namespace ResourceQuota (admission), so the "
        "Deployment cannot reach its desired replicas.", "confirmed", 0.9, evidence,
        factors=["New workload onboarded without quota headroom (hypothesis)", "Oversized requests (hypothesis)"],
        investigations=[D(f"kubectl -n {c.ns} describe resourcequota", "Used versus hard per resource."),
                        D(f"kubectl -n {c.ns} get pods -o custom-columns=NAME:.metadata.name,"
                          "CPU:.spec.containers[*].resources.requests.cpu,MEM:.spec.containers[*].resources.requests.memory",
                          "Which pods use the quota.")],
        remediation=[R(f"kubectl -n {c.ns} set resources deployment/{c.dep_name} --requests=cpu=<right-sized>",
                       "Right-size the new workload first (FinOps)."),
                     R("Raise the namespace quota through the platform's change process",
                       "Only with the budget owner's approval.")],
        verification=c.verify_rollout(), rollback=[c.undo()],
        expected_outcome="The ReplicaSet creates the missing pods.",
        risk="Low; raising quota allows more spend in the namespace.")


def check_node(c: Ctx) -> Finding | None:
    if not c.alert.node and not c.alert.alertname.lower().startswith(("kubenode", "node")):
        return None
    node = next((n for n in c.ev.nodes if n["name"] == c.alert.node), c.ev.nodes[0] if c.ev.nodes else None)
    if not node:
        return None
    cond = {x["type"]: x for x in node.get("conditions", [])}
    ready = cond.get("Ready", {})
    pressures = [t for t in ("MemoryPressure", "DiskPressure", "PIDPressure") if cond.get(t, {}).get("status") == "True"]
    instance = (node.get("provider_id") or "").rsplit("/", 1)[-1] or "<instance-id>"
    evidence = [Evidence(source="node.conditions", observation=f"{node['name']} Ready={ready.get('status')} "
                                                               f"reason={ready.get('reason')} message={ready.get('message')}"
                                                               f" (last heartbeat {ready.get('last_heartbeat')})")]
    for t in pressures:
        evidence.append(Evidence(source="node.conditions", observation=f"{t}=True: {cond[t].get('message')}"))
    if node.get("unschedulable"):
        evidence.append(Evidence(source="node.spec", observation="node is cordoned (unschedulable)"))
    usage = (c.ev.metrics.get("nodes") or {}).get(node["name"])
    if usage:
        evidence.append(Evidence(source="metrics", observation=f"usage {usage}, allocatable {node.get('allocatable')}"))
    aws = [D(f"aws ec2 describe-instance-status --instance-ids {instance} --include-all-instances",
             "Is the EC2 instance running, and do the system/instance status checks pass? (read-only AWS call)"),
           D("aws autoscaling describe-scaling-activities --max-items 5 --auto-scaling-group-name <node group ASG>",
             "Did the Auto Scaling group stop, replace or terminate the instance?")]
    if pressures:
        t = pressures[0]
        what = {"MemoryPressure": "memory", "DiskPressure": "disk space (root volume or image store)",
                "PIDPressure": "process IDs"}[t]
        return Finding(
            "NodePressure", f"Node {node['name']} is under {t}.",
            f"The kubelet reports {t}: the node is running out of {what}, and it evicts pods to protect itself.",
            "confirmed", 0.85, evidence,
            factors=["A pod without limits consumes the resource (hypothesis)",
                     "Log or image accumulation filling the disk (hypothesis)"],
            investigations=[D(f"kubectl describe node {node['name']}", "Conditions, allocated resources, events."),
                            D(f"kubectl top pods -A --sort-by={'memory' if t == 'MemoryPressure' else 'cpu'} | head -15",
                              "The heaviest pods.")] + aws,
            remediation=[R(f"kubectl cordon {node['name']}", "Stops new pods landing on the node while you investigate."),
                         R(f"kubectl drain {node['name']} --ignore-daemonsets --delete-emptydir-data",
                           "Moves workloads elsewhere; PodDisruptionBudgets are honoured. Then replace the node."),
                         R("Fix the offending workload's limits (memory/ephemeral-storage)", "Prevents recurrence.")],
            verification=[V(f"kubectl describe node {node['name']} | grep -A8 Conditions", f"{t} False.")],
            rollback=[B(f"kubectl uncordon {node['name']}", "Returns the node to service once healthy.")],
            expected_outcome="The pressure condition clears and evictions stop.",
            risk="Medium: draining moves pods; ensure other nodes have capacity first.",
            severity_floor="HIGH")
    if ready.get("status") != "True":
        unknown = ready.get("status") == "Unknown"
        cause = ("The kubelet on this node has stopped reporting to the control plane "
                 f"({ready.get('message') or ready.get('reason')}). The node is confirmed NotReady; why the kubelet "
                 "stopped is not visible from the Kubernetes API.") if unknown else (
                 f"The kubelet reports the node NotReady: {ready.get('reason')}: {ready.get('message')}.")
        return Finding(
            "NodeNotReady", f"Node {node['name']} is NotReady; its pods will be evicted after the toleration period "
                            "(default 5 minutes).", cause, "probable" if unknown else "confirmed",
            0.6 if unknown else 0.8, evidence,
            factors=["The EC2 instance was stopped or terminated, or failed its status checks (hypothesis; check AWS)",
                     "Network loss between the node and the control plane (security group, ENI) (hypothesis)",
                     "The kubelet or container runtime crashed (hypothesis; check via SSM)"],
            investigations=[D(f"kubectl describe node {node['name']}", "Conditions, last heartbeat and events."),
                            D(f"kubectl get pods -A -o wide --field-selector spec.nodeName={node['name']}",
                              "Which workloads are affected.")] + aws + [
                            D(f"aws ssm start-session --target {instance}  # then: sudo journalctl -u kubelet -n 100",
                              "Kubelet logs on the node (needs the SSM agent and an approved session).")],
            remediation=[R(f"kubectl drain {node['name']} --ignore-daemonsets --delete-emptydir-data",
                           "Moves workloads off the node (honours PodDisruptionBudgets)."),
                         R(f"aws ec2 terminate-instances --instance-ids {instance}",
                           "Lets the managed node group replace a broken instance. Only after drain and approval.")],
            verification=[V("kubectl get nodes -o wide", "All nodes Ready; the replacement node joined."),
                          V("kubectl get pods -A --field-selector status.phase!=Running", "No pods stuck Pending.")],
            rollback=[B(f"kubectl uncordon {node['name']}", "If the node recovers on its own.")],
            expected_outcome="The workloads run on healthy nodes and the node group is back at desired size.",
            risk="Medium: terminating an instance is irreversible; draining needs spare capacity.",
            severity_floor="HIGH")
    return None


def check_coredns(c: Ctx) -> Finding | None:
    cd = c.ev.coredns
    if not cd:
        return None
    avail = (cd.get("status") or {}).get("availableReplicas") or 0
    if avail > 0:
        return None
    evidence = [Evidence(source="kube-system/coredns",
                         observation=f"coredns deployment: desired {cd.get('replicas')}, available {avail}")]
    return Finding(
        "CoreDNSDown", "Cluster DNS (CoreDNS) has no available replicas: every name lookup in the cluster fails.",
        "CoreDNS has no running replicas" + (" because it was scaled to 0" if cd.get("replicas") == 0 else "") + ".",
        "confirmed", 0.95, evidence,
        factors=["A manual scale-down or a failed add-on update (check CloudTrail / audit log) (hypothesis)"],
        investigations=[D("kubectl -n kube-system get deploy coredns", "Desired vs available replicas."),
                        D("kubectl -n kube-system get events --sort-by=.lastTimestamp | tail -20", "Recent changes."),
                        D("aws eks describe-addon --cluster-name <cluster> --addon-name coredns",
                          "Managed add-on status (EKS may restore it).")],
        remediation=[R("kubectl -n kube-system scale deployment coredns --replicas=2",
                       "Restores DNS. On EKS the managed add-on may do this itself.")],
        verification=[V("kubectl -n kube-system rollout status deployment/coredns", "successfully rolled out"),
                      V("kubectl run dnscheck --rm -it --image=busybox:1.36 --restart=Never -- nslookup kubernetes.default",
                        "Resolves to the API service IP (creates a short-lived pod: run it in a test namespace).")],
        rollback=[],
        expected_outcome="Name resolution works again; dependent services recover without restarts.",
        risk="Low: restoring replicas is safe.", severity_floor="CRITICAL")


def check_service_endpoints(c: Ctx) -> Finding | None:
    zero = [s for s in c.ev.services if s.get("ready_endpoints") == 0]
    if not zero or not any(ct.get("ready") for ct in c.containers()):
        return None
    s = zero[0]
    return Finding(
        "ServiceNoEndpoints", f"Service {s['name']} has no ready endpoints although pods are Ready.",
        "The Service's selector or targetPort does not match the Ready pods, so traffic to it fails.",
        "probable", 0.6, [Evidence(source="services", observation=f"{s['name']}: selector {s['selector']}, "
                                                                  f"ports {s['ports']}, 0 ready endpoints")],
        investigations=[D(f"kubectl -n {c.ns} get endpoints {s['name']}", "Empty ADDRESSES confirms it."),
                        D(f"kubectl -n {c.ns} get pods --show-labels", "Compare pod labels with the selector.")],
        remediation=[R(f"kubectl -n {c.ns} edit service {s['name']}",
                       "Fix the selector (or targetPort) to match the pods. " + (GITOPS_NOTE if c.gitops else ""))],
        verification=[V(f"kubectl -n {c.ns} get endpoints {s['name']}", "Pod IPs listed.")],
        expected_outcome="Requests to the Service reach the pods again.", risk="Low.")


def check_throttling(c: Ctx) -> Finding | None:
    if c.alert.alertname not in ("CPUThrottlingHigh", "ContainerCpuThrottled"):
        return None
    ct = next((x for x in c.containers() if x["name"] == c.alert.container), c.containers()[0] if c.containers() else None)
    if not ct:
        return None
    lim = ((ct.get("resources") or {}).get("limits") or {}).get("cpu", "not set")
    usage = ((c.ev.metrics.get("pods") or {}).get(c.pod_name) or {}).get(ct["name"], {}).get("cpu")
    evidence = [Evidence(source="alert", observation=c.alert.description or c.alert.summary or c.alert.alertname),
                Evidence(source="pod.spec", observation=f"container {ct['name']} CPU limit {lim}")]
    if usage:
        evidence.append(Evidence(source="metrics", observation=f"current CPU usage {usage}"))
    return Finding(
        "CPUThrottling", f"{ct['name']} in {c.pod_name} is CPU-throttled.",
        f"The container regularly hits its CPU limit ({lim}) and the kernel throttles it, adding latency.",
        "probable", 0.6, evidence,
        factors=["The limit is too low for peak load (hypothesis)", "Bursty start-up (JIT, cache warm-up) (hypothesis)"],
        investigations=[D(f"kubectl -n {c.ns} top pod {c.pod_name} --containers", "CPU usage versus limit.")],
        remediation=[R(f"kubectl -n {c.ns} set resources deployment/{c.dep_name} -c {ct['name']} --limits=cpu=<higher>",
                       "Raise (or remove, per platform policy) the CPU limit based on measured peaks. "
                       + (GITOPS_NOTE if c.gitops else ""))],
        verification=c.verify_rollout(), rollback=[c.undo()],
        expected_outcome="The throttling ratio drops below the alert threshold and latency improves.",
        risk="Low; higher limits let the pod use more node CPU.")


CHECKERS: list[Callable[[Ctx], Finding | None]] = [
    check_coredns, check_node, check_config_error, check_image_pull, check_oom, check_liveness, check_crashloop,
    check_pvc, check_quota, check_pending, check_readiness, check_service_endpoints, check_throttling,
]


def inconclusive(c: Ctx) -> Finding:
    evidence: list[Evidence] = []
    for p in c.ev.pods[:2]:
        evidence.append(Evidence(source="pod.status", observation=f"pod {p['name']} phase {p.get('phase')}, containers "
                                 + ", ".join(f"{x['name']}={x['state']} ready={x.get('ready')} restarts="
                                             f"{x.get('restart_count')}" for x in p.get("containers", []))))
    if c.ev.deployment:
        evidence.append(Evidence(source="deployment.status", observation=str(c.ev.deployment.get("status"))))
    warnings = [e for e in c.ev.events if e.get("type") == "Warning"]
    evidence += [c.ev_event(e) for e in warnings[-3:]]
    return Finding(
        "unknown", c.alert.summary or f"Alert {c.alert.alertname} fired; the collected evidence shows no known failure pattern.",
        INCONCLUSIVE, "inconclusive", 0.2 if evidence else 0.0, evidence,
        factors=["The condition may have been transient and already cleared (hypothesis)",
                 "The cause may be outside Kubernetes (a dependency, the network, the cloud provider) (hypothesis)"],
        investigations=[
            D(f"kubectl -n {c.ns} get events --sort-by=.lastTimestamp | tail -30", "Recent events in the namespace."),
            D(f"kubectl -n {c.ns} get pods -o wide", "Current pod status."),
            D("Open the Grafana dashboards for the namespace/pod around the alert time",
              "Metrics history shows what changed (traffic, latency, memory)."),
            D("Check the Argo CD history and Git log for changes just before the alert", "Correlate with releases.")]
        + ([c.describe_pod()] if c.pod else []),
        expected_outcome="Further evidence identifies the cause, or the alert clears on its own.",
        risk="None from investigating; do not apply changes until the cause is known.")


# ---------------------------------------------------------------------- report assembly

def business_impact(c: Ctx, finding: Finding) -> str:
    if finding.incident_type == "CoreDNSDown":
        return "Cluster-wide: every service that resolves names fails. Treat as a major incident."
    if finding.incident_type in ("NodeNotReady", "NodePressure"):
        return (f"Workloads on node {c.alert.node or '?'} ({c.ev.targets.get('pods_on_node', 'unknown number of')} pods) "
                "are evicted or degraded; replicated tier-1 services should keep serving from other nodes.")
    labels = (c.dep or {}).get("labels") or (c.pod or {}).get("labels") or {}
    tier, team = labels.get("bank.sim/tier"), labels.get("bank.sim/owner-team")
    exposure = labels.get("bank.sim/exposure")
    st = (c.dep or {}).get("status") or {}
    parts = []
    if c.dep:
        parts.append(f"{c.dep_name}: {st.get('availableReplicas') or 0} of {c.dep.get('replicas')} replicas available.")
    zero = [s["name"] for s in c.ev.services if s.get("ready_endpoints") == 0]
    if zero:
        parts.append(f"Service(s) {', '.join(zero)} have no ready endpoints: requests to them fail.")
    elif c.ev.services:
        parts.append("The Service still has ready endpoints, so traffic is served by the remaining pods.")
    if tier:
        parts.append(f"{tier}{' customer-facing' if exposure == 'edge' else ''} service owned by {team or 'unknown team'}.")
    return " ".join(parts) or "Impact unknown: no workload or Service information was collected."


def build_report(alert: NormalizedAlert, ev: InvestigationEvidence, incident_id: str) -> IncidentReport:
    c = Ctx(alert, ev)
    finding = None
    notes: list[str] = []
    for check in CHECKERS:
        try:
            finding = check(c)
        except Exception as e:  # a checker bug must not lose the incident
            notes.append(f"checker {check.__name__} failed: {type(e).__name__}: {e}")
            finding = None
        if finding:
            break
    finding = finding or inconclusive(c)

    severity = alert.severity
    if finding.severity_floor and severity_rank(finding.severity_floor) < severity_rank(severity):
        severity = finding.severity_floor

    affected = []
    if c.dep:
        affected.append(f"deployment/{c.dep_name}")
    affected += [f"pod/{p['name']}" for p in ev.pods]
    if alert.node:
        affected.append(f"node/{alert.node}")
    affected += [f"pvc/{p['name']}" for p in ev.pvcs]
    if alert.container:
        affected.append(f"container/{alert.container}")

    symptoms = [f"Alert {alert.alertname}: {alert.summary or alert.description}".strip(": ")]
    for p in ev.pods:
        for ct in p.get("containers", []):
            if not ct.get("ready") or ct.get("restart_count"):
                symptoms.append(f"{p['name']}/{ct['name']}: {ct['state']}, ready={ct.get('ready')}, "
                                f"restarts={ct.get('restart_count')}")
    if ev.errors:
        notes += [f"evidence gap: {e}" for e in ev.errors[:10]]
    if ev.truncated:
        notes.append("investigation hit its time or size limit; evidence may be incomplete")

    steps = [ResolutionStep(title="1. Confirm the symptom and the evidence (read-only)",
                            explanation="Reproduce what the agent saw before changing anything.",
                            commands=finding.investigations)]
    if finding.remediation:
        steps.append(ResolutionStep(
            title="2. Apply the fix (human, after approval)",
            explanation=("These commands were NOT executed by the agent. Run them only after the approval in the "
                         "incident dashboard and your change process. " + (GITOPS_NOTE if c.gitops else "")).strip(),
            commands=finding.remediation))
    if finding.verification:
        steps.append(ResolutionStep(title=f"{len(steps) + 1}. Verify the resolution (read-only)",
                                    commands=finding.verification))
    if finding.rollback:
        steps.append(ResolutionStep(title=f"{len(steps) + 1}. If it gets worse: roll back (human)",
                                    commands=finding.rollback))

    report = IncidentReport(
        incident_id=incident_id,
        severity=severity,
        cluster=alert.cluster,
        namespace=alert.namespace,
        affected_resources=list(dict.fromkeys(affected)),
        incident_type=finding.incident_type,
        incident_summary=finding.summary,
        symptoms=symptoms,
        root_cause=finding.root_cause,
        root_cause_status=finding.status,  # type: ignore[arg-type]
        root_cause_confidence=finding.confidence,
        supporting_evidence=finding.evidence,
        possible_contributing_factors=finding.factors,
        business_impact=business_impact(c, finding),
        recommended_investigations=finding.investigations,
        resolution_steps=steps,
        commands_for_human_execution=finding.remediation,
        prerequisites=finding.prerequisites,
        expected_outcome=finding.expected_outcome,
        verification_steps=finding.verification,
        rollback_guidance=finding.rollback,
        risk_assessment=finding.risk,
        agent_actions_taken=[f"read-only: {cmd}" for cmd in ev.commands_executed],
        analysis_engine="rules",
        analysis_notes=notes,
        started_at=alert.starts_at,
    )
    return report.enforce_invariants()
