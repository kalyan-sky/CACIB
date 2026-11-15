"""Deterministic root cause analysis.

Each checker looks at the collected evidence for one failure pattern. A checker
only claims a root cause when an observed fact supports it (an event message, a
termination reason, a log line, a node condition), and it cites that fact as
evidence. Anything it infers beyond the facts goes into the contributing
factors as a hypothesis. If no checker matches, the result is inconclusive.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from . import runbooks
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

# ---------------------------------------------------------------------- quantities (for ready-to-run values)

_MEM = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "K": 10**3, "M": 10**6, "G": 10**9, "k": 10**3}


def mem_bytes(q: str | None) -> int | None:
    m = re.fullmatch(r"([0-9.]+)([KMGT]i?|k)?", str(q or "").strip())
    return int(float(m.group(1)) * _MEM.get(m.group(2) or "", 1)) if m else None


def fmt_mem(b: float, step_mi: int = 32) -> str:
    """Round up to a multiple of step_mi Mi (or whole Gi above 2 Gi)."""
    mi = b / 2**20
    if mi > 2048:
        return f"{int(-(-mi // 1024))}Gi"
    return f"{int(-(-mi // step_mi) * step_mi)}Mi"


def cpu_milli(q: str | None) -> int | None:
    q = str(q or "").strip()
    if q.endswith("n"):
        return max(1, int(q[:-1]) // 1_000_000)
    if q.endswith("m"):
        return int(q[:-1])
    try:
        return int(float(q) * 1000)
    except ValueError:
        return None


def fmt_cpu(m: float) -> str:
    m = max(10, int(-(-m // 10) * 10))
    return f"{m // 1000}" if m % 1000 == 0 else f"{m}m"


def ask(var: str, prompt: str, secret: bool = False) -> str:
    """A value only a human can know: prompted at run time (never echoed for secrets), never a <placeholder>."""
    flag = "-rsp" if secret else "-rp"
    return f'read {flag} "{prompt}: " {var}' + (" && echo" if secret else "") + f' && test -n "${var}"'


def ask_default(var: str, prompt: str, default: str) -> str:
    """Prompt with a computed default: Enter accepts it."""
    return f'read -rp "{prompt} [{default}]: " {var}; {var}="${{{var}:-{default}}}"'


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

CERT_ALERT_PREFIXES = ("Certificate", "TLSEndpoint", "X509", "TLS")


def check_certificate(c: Ctx) -> Finding | None:
    """Expired, expiring, wrong-host or untrusted TLS certificates.

    Evidence: what the endpoint presents in a real handshake (agent TLS probe) and/or what the
    certificate exporter read from the Secret (in the alert). The agent never reads the Secret.
    """
    a, tls = c.alert, c.ev.tls
    if not a.alertname.startswith(CERT_ALERT_PREFIXES) and not tls:
        return None
    secret = a.secret or next((s for p in c.ev.pods for s in p.get("secrets_mounted", []) if "tls" in s), None)
    sname = secret or "<tls-secret>"
    evidence: list[Evidence] = []
    if tls and not tls.get("connect_error"):
        evidence.append(Evidence(
            source="tls.handshake",
            observation=f"{tls['endpoint']} presents a certificate for CN={tls.get('subject_cn')}, DNS names "
                        f"{tls.get('dns_names')}, issued by {tls.get('issuer_cn')}, valid until {tls.get('not_after')} "
                        f"({tls.get('days_left')} days left)"))
        evidence.append(Evidence(source="tls.verify", observation="TLS verification succeeded" if tls.get("verified")
                                 else f"TLS verification failed: {tls.get('verify_error')}"))
    elif tls:
        evidence.append(Evidence(source="tls.handshake", observation=f"no TLS handshake: {tls.get('connect_error')}"))
    if a.alertname.startswith(CERT_ALERT_PREFIXES):
        text = a.description or a.summary
        if text:
            evidence.append(Evidence(source="alert", observation=f"{a.alertname}: {text}"))
    if c.ev.secret_consumers:
        evidence.append(Evidence(source="pod.spec/ingress",
                                 observation=f"Secret {c.ns}/{sname} is used by {', '.join(c.ev.secret_consumers)}"))
    for p in c.ev.pods[:1]:
        if p.get("secrets_mounted"):
            evidence.append(Evidence(source="pod.spec", observation=f"pod {p['name']} mounts Secret(s) "
                                                                    f"{', '.join(p['secrets_mounted'])}"))

    host = (tls or {}).get("hostname") or "<host>"
    port = ((tls or {}).get("endpoint") or ":443").rsplit(":", 1)[-1]
    consumer = next((u.split("/", 1)[1] for u in c.ev.secret_consumers if u.startswith("deployment/")), None) \
        or (c.dep_name if c.dep_name != "<deployment>" else "<deployment>")
    show_cert = D(f"kubectl -n {c.ns} get secret {sname} -o jsonpath='{{.data.tls\\.crt}}' | base64 -d | "
                  "openssl x509 -noout -subject -issuer -dates -ext subjectAltName",
                  "Shows the public certificate stored in the Secret (run by an engineer; the agent cannot read Secrets).",
                  "notAfter and the DNS names; compare with what the endpoint presents.")
    svc = host.split(".")[0] if host.endswith((".svc", ".svc.cluster.local")) else "<service>"
    names = sorted({host, *((tls or {}).get("dns_names") or [])} - {"<host>"}) if tls else []
    ca_file = os.environ.get("SRE_AGENT_GUIDE_CA_FILE", "internal-ca.pem")  # where engineers keep the internal CA
    if tls and not tls.get("hostname_matches") and host != "<host>":
        names = [host, f"{host}.cluster.local"] if host.endswith(".svc") else [host]  # the names clients use
    san = ",".join(f"DNS:{n}" for n in names) or "DNS:<name-clients-use>"
    cn = names[0] if names else "<name-clients-use>"
    # Every command is runnable as written (placeholders in <>), or a "#" comment that is harmless to paste.
    # Verification runs from the engineer's machine: trusted internal CA + hostname check, no pod created.
    check = (f"kubectl -n {c.ns} port-forward svc/{svc} 18443:{port} >/dev/null 2>&1 & sleep 3; "
             f"openssl s_client -connect 127.0.0.1:18443 -servername {host} -verify_hostname {host} "
             f"-CAfile {os.environ.get('SRE_AGENT_GUIDE_CA_FILE', 'internal-ca.pem')} -verify_return_error </dev/null 2>/dev/null "
             "| grep -E 'subject=|NotAfter|Verify return code'; kill %1")
    probe_cmd = D(check, "Repeats the handshake a verifying client makes, with the hostname check; "
                         "'Verify return code' shows the exact failure.")
    replace = [
        R(f'openssl req -new -newkey rsa:2048 -nodes -keyout new.key -out new.csr -subj "/CN={cn}" '
          f'-addext "subjectAltName={san}"',
          "Creates a new private key and a certificate request with every name clients use.",
          "new.key and new.csr"),
        R("# Send new.csr to the issuing CA (bank PKI team, ACM Private CA, or let cert-manager re-issue it); "
          "save the signed certificate followed by its intermediates as new.crt",
          "An organisational step, not a command. Never self-sign a production certificate."),
        R("openssl x509 -noout -subject -enddate -ext subjectAltName -in new.crt",
          "Check the names and dates of the new certificate before deploying it."),
        R(f"kubectl -n {c.ns} get secret {sname} -o yaml > {sname}.backup.yaml && "
          f"kubectl -n {c.ns} create secret tls {sname} --cert=new.crt --key=new.key --dry-run=client -o yaml "
          f"| kubectl apply -f -",
          "Backs up the current Secret (for rollback; store it securely, it contains the old key), then replaces "
          "the certificate and key.", f"secret/{sname} configured"),
        R(f"kubectl -n {c.ns} rollout restart deployment/{consumer} && "
          f"kubectl -n {c.ns} rollout status deployment/{consumer}",
          "Most servers (nginx, Java) read the certificate only at start-up.",
          f'deployment "{consumer}" successfully rolled out'),
    ]
    verify = [V(check, "The handshake must verify for the name clients use.", "Verify return code: 0 (ok)"),
              V("# Wait for the certificate alerts to resolve (checks run every 30-60 s); the RCA email follows",
                "Monitoring confirms the fix from the clients' point of view.")]
    rollback = [B(f"kubectl apply -f {sname}.backup.yaml && kubectl -n {c.ns} rollout restart deployment/{consumer}",
                  "Only if the new certificate breaks clients and the old one is still valid. Delete the backup "
                  "file afterwards (it contains a private key).")]
    common = dict(investigations=[show_cert, probe_cmd], verification=verify, rollback=rollback)
    renewal_factors = ["Renewal is manual and nobody owned the expiry date (hypothesis)",
                       "An automated renewal (cert-manager/ACM) failed: check its status and events (hypothesis)"]

    verify_error = ((tls or {}).get("verify_error") or "").lower()
    expired_alert = "expired" in a.alertname.lower()
    expiry_alert = "expir" in a.alertname.lower() and not expired_alert
    endpoint_expired = bool(tls) and not tls.get("connect_error") and "expired" in verify_error  # type: ignore[union-attr]
    if endpoint_expired or (expired_alert and not tls):
        cn = (tls or {}).get("subject_cn") or a.labels.get("subject_CN") or "?"
        where = host if endpoint_expired else f"Secret {c.ns}/{sname}"
        return Finding("CertificateExpired",
                       f"{where} holds an expired certificate: every client that verifies TLS is rejected.",
                       (f"The certificate presented by {host} (CN={cn}) expired at {tls.get('not_after')}. "  # type: ignore[union-attr]
                        if endpoint_expired else f"The certificate CN={cn} in Secret {c.ns}/{sname} has expired. ")
                       + "It was not renewed before its expiry date.",
                       "confirmed", 0.95, evidence, factors=renewal_factors, remediation=replace,
                       prerequisites=["A new certificate and key from the issuing CA", "A change window (restart)"],
                       expected_outcome="Clients verify the certificate again; the alerts resolve.",
                       risk="Low for a like-for-like renewal. Wrong DNS names or chain would break clients again.",
                       severity_floor="CRITICAL", **common)  # type: ignore[arg-type]
    if tls and not tls.get("connect_error") and ("hostname mismatch" in verify_error or
                                                  (tls.get("hostname_matches") is False and not tls.get("verified"))):
        return Finding("TLSHostnameMismatch",
                       f"{host} serves a certificate issued for other names: clients reject the connection.",
                       f"Clients connect to {host}, but the certificate (CN={tls.get('subject_cn')}) is only valid "
                       f"for {tls.get('dns_names') or 'its CN'}. The wrong certificate is deployed, or it was issued "
                       f"without {host} in its DNS names.",
                       "confirmed", 0.95, evidence,
                       factors=["The certificate of another service was copied into this Secret (hypothesis)",
                                "The certificate request listed only the external name, not the service name "
                                "clients use (hypothesis)"],
                       remediation=replace,
                       prerequisites=[f"A certificate whose DNS names include {host}"],
                       expected_outcome="The handshake verifies for every name clients use.",
                       risk="Low; list every name clients use in the new certificate.",
                       severity_floor="HIGH", **common)  # type: ignore[arg-type]
    if tls and not tls.get("connect_error") and any(k in verify_error for k in
                                                     ("unable to get local issuer", "self-signed", "self signed",
                                                      "unable to verify the first certificate")):
        return Finding("TLSUntrustedCertificate",
                       f"{host} serves a certificate clients cannot trace to a trusted CA.",
                       f"The certificate issued by {tls.get('issuer_cn')} does not chain to a CA the client trusts "
                       f"({tls.get('verify_error')}): the intermediate certificate is missing from tls.crt, or the "
                       "issuing CA is not in the clients' trust store.",
                       "confirmed", 0.85, evidence,
                       factors=["tls.crt holds only the leaf, without the intermediate (hypothesis)",
                                "A private CA that clients were never given (hypothesis)"],
                       remediation=[R(f"cat leaf.crt intermediate.crt > fullchain.crt && kubectl -n {c.ns} create "
                                      f"secret tls {sname} --cert=fullchain.crt --key=tls.key --dry-run=client -o yaml "
                                      "| kubectl apply -f -", "Serve the full chain: leaf first, then intermediates."),
                                    replace[4]],
                       expected_outcome="Clients build the chain to a trusted root.",
                       risk="Low.", severity_floor="HIGH", **common)  # type: ignore[arg-type]
    if (tls and tls.get("verified") and (tls.get("days_left") or 999) < 30) or expiry_alert:
        days = (tls or {}).get("days_left")
        when = f"{tls.get('not_after')} ({days} days left)" if tls and days is not None else \
            (a.description or a.summary or "soon (see the alert)")
        subject = f"CN={tls.get('subject_cn')}" if tls else (a.labels.get("subject_CN") and
                                                            f"CN={a.labels.get('subject_CN')}") or "the certificate"
        return Finding("CertificateExpiringSoon",
                       f"The TLS certificate {subject} in Secret {c.ns}/{sname} expires soon; on that date clients "
                       "will be rejected.",
                       f"{subject} expires at {when} and has not been renewed yet.",
                       "confirmed", 0.9, evidence, factors=renewal_factors, remediation=replace,
                       prerequisites=["A new certificate and key from the issuing CA before the expiry date"],
                       expected_outcome="The new certificate is served; the expiry alert resolves.",
                       risk="Low if done before expiry; after expiry this becomes an outage.", **common)  # type: ignore[arg-type]
    if tls and tls.get("connect_error"):
        return Finding("TLSEndpointUnreachable", f"{tls.get('endpoint')} does not complete a TLS handshake.",
                       f"The TLS connection fails before a certificate is exchanged: {tls.get('connect_error')}.",
                       "probable", 0.6, evidence,
                       factors=["The pods behind the Service are down (hypothesis)",
                                "The port is not TLS, or a NetworkPolicy blocks it (hypothesis)"],
                       investigations=[probe_cmd, c.get_pods()],
                       expected_outcome="The endpoint completes the handshake.", risk="None from investigating.")
    return None


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
                R((ask("VALUE", f"Value for key {key} in {kind} {obj} (from the secret owner)", secret=True)
                   + f" && kubectl -n {c.ns} patch {fix_obj} --type json -p \"[{{\\\"op\\\":\\\"add\\\","
                   f"\\\"path\\\":\\\"/data/{key}\\\",\\\"value\\\":\\\"$(printf %s \"$VALUE\" | base64 | tr -d '\\n')\\\"}}]\""
                   "; unset VALUE") if kind == "Secret" else
                  (ask("VALUE", f"Value for key {key} in ConfigMap {obj}")
                   + f" && kubectl -n {c.ns} create configmap {obj} --from-literal={key}=\"$VALUE\" --dry-run=client "
                   f"-o yaml | kubectl patch configmap {obj} -n {c.ns} --type merge --patch-file /dev/stdin; unset VALUE"),
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
                R((ask("KEY", f"Key name the pod expects in {kind} {obj}") + " && "
                   + ask("VALUE", "Its value (from the owner)", secret=kind.lower() == "secret")
                   + f" && kubectl -n {c.ns} create {'secret generic' if kind.lower() == 'secret' else 'configmap'} {obj} "
                   '--from-literal="$KEY=$VALUE"; unset KEY VALUE'),
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
    # Previous revision that used a different image: a known-good value, not a guess.
    previous = next((img for r in sorted(c.ev.replicasets, key=lambda r: int(r.get("revision") or 0), reverse=True)
                     for img in r.get("images", []) if img and img != image), None)
    if previous:
        fix = R(f"kubectl -n {c.ns} set image deployment/{c.dep_name} {ct['name']}={previous}",
                f"Returns to the image of the previous revision ({previous}), which was running before this "
                "change. " + (GITOPS_NOTE if c.gitops else ""), f"deployment.apps/{c.dep_name} image updated")
    else:
        fix = R(ask("NEW_IMAGE", f"Correct image for {ct['name']} (currently {image}; must exist in the registry)")
                + f' && kubectl -n {c.ns} set image deployment/{c.dep_name} {ct["name"]}="$NEW_IMAGE"',
                "No earlier working image is known (first deployment), so it asks for the tag that exists. "
                + (GITOPS_NOTE if c.gitops else ""), f"deployment.apps/{c.dep_name} image updated")
    return Finding(
        "ImagePullBackOff", f"{c.pod_name} cannot start: the image '{image}' cannot be pulled.", cause, status, conf,
        evidence,
        factors=["A typo in the image tag in the last change (hypothesis)",
                 "The tag was never pushed because the build failed (hypothesis)"],
        investigations=[c.describe_pod(),
                        D(f"crane ls {repo}  # ECR: aws ecr describe-images --repository-name {repo.split('/', 1)[-1]}",
                          "Lists the tags that exist in the registry (run from a machine with registry access).",
                          f"Check whether '{tag}' is in the list.")],
        remediation=[fix],
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
            lim_b, req_b = mem_bytes(limit), mem_bytes(((ct.get("resources") or {}).get("requests") or {}).get("memory"))
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
                remediation=[oom_fix(c, ct, lim_b, req_b, mem_bytes(usage))],
                verification=c.verify_rollout() + [V(f"kubectl -n {c.ns} get pod -l {c.selector()} "
                                                     "-o custom-columns=POD:.metadata.name,RESTARTS:.status.containerStatuses[0].restartCount",
                                                     "Restart counts stay the same for 15+ minutes.")],
                rollback=[c.undo()],
                prerequisites=["Memory usage history (Grafana: Kubernetes / Compute Resources / Pod)",
                               "Free capacity in the namespace ResourceQuota for a higher request"],
                expected_outcome="No more OOMKilled restarts; memory stays below the new limit.",
                risk="Medium: a higher limit uses more node memory and quota; a leak will still exhaust it later.")
    return None


def oom_fix(c: Ctx, ct: dict[str, Any], lim_b: int | None, req_b: int | None, use_b: int | None) -> Command:
    """Memory from evidence: limit = max(2x the old limit, 1.5x usage); request = max(1.2x usage, old request)."""
    base = lim_b or use_b or 128 * 2**20
    new_lim = max(base * 2, (use_b or 0) * 1.5)
    new_req = min(max((use_b or 0) * 1.2, req_b or 0, base * 0.75), new_lim)
    why = (f"Limit {fmt_mem(new_lim)} = max(2 x the current limit, 1.5 x measured usage); request {fmt_mem(new_req)} "
           "keeps the scheduler honest. A stop-gap if usage keeps growing (leak): then fix or roll back.")
    return R(f"kubectl -n {c.ns} set resources deployment/{c.dep_name} -c {ct['name']} "
             f"--requests=memory={fmt_mem(new_req)} --limits=memory={fmt_mem(new_lim)}",
             why + (" " + GITOPS_NOTE if c.gitops else ""), f"deployment.apps/{c.dep_name} resource requirements updated")


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
                    remediation.append(R("# Restore the dependency first (see its own incident), then let the pod retry",
                                         "Restarting this pod does not help while the dependency is down."))
                    factors.append("The dependency is down, or a NetworkPolicy blocks the connection (hypothesis)")
                elif category == "memory":
                    lim = mem_bytes(((ct.get("resources") or {}).get("limits") or {}).get("memory"))
                    remediation.append(oom_fix(c, ct, lim, None, None))
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
        remediation=[R(ask("PROBE_PATH", f"Health path {not_ready[0]['name']} really serves (e.g. /healthz)")
                       + f" && kubectl -n {c.ns} patch deployment {c.dep_name} -p "
                       f"\"{{\\\"spec\\\":{{\\\"template\\\":{{\\\"spec\\\":{{\\\"containers\\\":[{{\\\"name\\\":"
                       f"\\\"{not_ready[0]['name']}\\\",\\\"readinessProbe\\\":{{\\\"httpGet\\\":"
                       f"{{\\\"path\\\":\\\"$PROBE_PATH\\\"}}}}}}]}}}}}}}}\"",
                       "Points the readiness probe at the path the app answers (check: kubectl port-forward and curl "
                       "it). Strategic merge by container name, so other settings stay. "
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
    default = next((s.split(" ")[0] for s in c.ev.storage_classes if "(default)" in s),
                   (c.ev.storage_classes[0].split(" ")[0] if c.ev.storage_classes else None))
    modes = ", ".join(pvc.get("access_modes") or ["ReadWriteOnce"])
    manifest = (f"kubectl -n {c.ns} apply -f - <<'EOF'\napiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n"
                f"  name: {pvc['name']}\nspec:\n  storageClassName: {default}\n  accessModes: [{modes}]\n"
                f"  resources:\n    requests:\n      storage: {pvc.get('requested') or '1Gi'}\nEOF")
    return Finding(
        "PersistentVolumeClaimPending", f"PVC {pvc['name']} is Pending, so pods that mount it cannot start.", cause,
        status, conf, evidence,
        factors=["The manifest was written for another cluster with different StorageClass names (hypothesis)"],
        investigations=[D(f"kubectl -n {c.ns} describe pvc {pvc['name']}", "The provisioning events."),
                        D("kubectl get storageclass", "Which classes exist and which is the default.")],
        remediation=([R(f"kubectl -n {c.ns} get pvc {pvc['name']} -o yaml > {pvc['name']}-pvc.backup.yaml",
                        "Keeps the original definition (for rollback and the record)."),
                      R(f"test \"$(kubectl -n {c.ns} get pvc {pvc['name']} -o jsonpath='{{.status.phase}}')\" = Pending && "
                        f"kubectl -n {c.ns} delete pvc {pvc['name']} --wait=true",
                        "storageClassName is immutable, so the claim is recreated. The test refuses to delete it "
                        "unless it is still Pending (a Pending claim holds no data)."),
                      R(manifest, f"Recreates the claim with the existing StorageClass {default} "
                                  f"(same name, size {pvc.get('requested') or '1Gi'} and access modes), so the "
                                  "waiting pod mounts it.")] if default else
                     [R("# No StorageClass exists in this cluster: install a CSI driver / StorageClass first",
                        "Nothing can provision volumes until then.")]),
        verification=[V(f"kubectl -n {c.ns} get pvc {pvc['name']}", "STATUS Bound."),
                      V(f"kubectl -n {c.ns} get pods -o wide", "The pod that mounts it starts.")],
        rollback=[B(f"kubectl -n {c.ns} delete pvc {pvc['name']} && kubectl -n {c.ns} create -f {pvc['name']}-pvc.backup.yaml",
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
        # A value that is guaranteed to fit: a quarter of the largest node's allocatable.
        allocs = [(cpu_milli if res == "cpu" else mem_bytes)(n["allocatable"].get(res)) for n in c.ev.nodes
                  if n.get("allocatable")]
        biggest = max([a for a in allocs if a] or [0])
        fits = (fmt_cpu(biggest / 4) if res == "cpu" else fmt_mem(biggest / 4)) if biggest else \
            ("250m" if res == "cpu" else "256Mi")
        remediation = [R(ask_default("REQ", f"{res} request for {ct.get('name')} (measured usage + headroom)", fits)
                         + f' && kubectl -n {c.ns} set resources deployment/{c.dep_name} -c {ct.get("name")} '
                           f'--requests={res}="$REQ"',
                         f"Default {fits} fits on a node (a quarter of the largest node's allocatable {res}); "
                         "type the measured usage plus headroom if you know it (Grafana / kubectl top). "
                         + (GITOPS_NOTE if c.gitops else "")),
                       R("# Only if the workload really needs that much: scale the node group (EKS console > Compute "
                         "> node group > Edit) or use Karpenter", "Adds capacity; costs money.")]
        status, conf = "confirmed", 0.9
    elif "exceed the max Fargate configuration" in msg:
        cause = (f"The pod's requests ({req}) are larger than the largest Fargate size (16 vCPU / 120 GB), "
                 "so Fargate can never place it.")
        remediation = [R(ask_default("CPU", "CPU request (measured usage + headroom)", "250m") + "; "
                         + ask_default("MEM", "Memory request", "512Mi")
                         + f' && kubectl -n {c.ns} set resources deployment/{c.dep_name} -c {ct.get("name")} '
                           '--requests=cpu="$CPU",memory="$MEM"',
                         "Fargate's largest pod is 16 vCPU / 120 GB; the defaults are its smallest size.")]
        status, conf = "confirmed", 0.9
    elif any(k in msg for k in ("didn't match Pod's node affinity/selector", "untolerated taint", "had taint")):
        sel_hit = "didn't match Pod's node affinity/selector" in msg
        taint_hit = "untolerated taint" in msg or "had taint" in msg
        selector = p.get("node_selector") or {}
        dedicated = [n for n in c.ev.nodes if n.get("taints") and selector
                     and all((n.get("labels") or {}).get(k) == v for k, v in selector.items())]
        if taint_hit and dedicated:
            sel_hit = False  # the selector does find its node; only the taint keeps the pod off it
        causes, remediation = [], []
        if sel_hit:
            evidence.append(Evidence(source="pod.spec", observation=f"nodeSelector {p.get('node_selector') or '{}'}, "
                                                                    f"affinity set: {p.get('affinity')}"))
            labels = {n["name"]: n.get("labels") for n in c.ev.nodes[:3]}
            if labels:
                evidence.append(Evidence(source="nodes", observation=f"node labels (sample): {labels}"))
            causes.append(f"its nodeSelector/affinity ({p.get('node_selector') or 'affinity rules'}) matches no "
                          "untainted node")
            remediation.append(R(ask("SELECTOR_KEY", "Node label key the target nodes really have") + " && "
                                 + ask("SELECTOR_VALUE", "Its value")
                                 + f" && kubectl -n {c.ns} patch deployment {c.dep_name} --type json -p "
                                 "\"[{\\\"op\\\":\\\"replace\\\",\\\"path\\\":\\\"/spec/template/spec/nodeSelector\\\","
                                 "\\\"value\\\":{\\\"$SELECTOR_KEY\\\":\\\"$SELECTOR_VALUE\\\"}}]\"",
                                 "Points the nodeSelector at a label the intended nodes have (kubectl get nodes "
                                 "--show-labels). Label nodes through the node group, not by hand. "
                                 + (GITOPS_NOTE if c.gitops else "")))
        if taint_hit:
            taints = {n["name"]: n.get("taints") for n in c.ev.nodes if n.get("taints")}
            evidence.append(Evidence(source="nodes", observation=f"node taints: {taints or 'see the event'}"))
            evidence.append(Evidence(source="pod.spec", observation=f"pod tolerations: {p.get('tolerations') or 'none'}"))
            causes.append("the nodes it is meant for carry a taint it does not tolerate")
            target = (dedicated or [n for n in c.ev.nodes if n.get("taints")] or [{}])[0]
            taint = next((t for t in target.get("taints") or [] if t.get("effect") in ("NoSchedule", "NoExecute")), None)
            if taint:
                tol = [*(p.get("tolerations") or []),
                       {"key": taint["key"], "operator": "Equal", "value": taint.get("value", ""),
                        "effect": taint["effect"]}]
                patch = json.dumps({"spec": {"template": {"spec": {"tolerations": tol}}}}, separators=(",", ":"))
                remediation.append(R(f"kubectl -n {c.ns} patch deployment {c.dep_name} -p '{patch}'",
                                     f"Tolerates {taint['key']}={taint.get('value', '')}:{taint['effect']} on "
                                     f"{target.get('name')}, keeping existing tolerations. Do not remove the taint: "
                                     "it keeps other workloads off dedicated nodes. " + (GITOPS_NOTE if c.gitops else "")))
        cause = "The scheduler finds no node for the pod: " + "; and ".join(causes) + "."
        status, conf = "confirmed", 0.9 if len(causes) == 1 else 0.85
    elif "unbound immediate PersistentVolumeClaims" in msg or "persistentvolumeclaim" in msg.lower():
        cause = "The pod waits for a PersistentVolumeClaim that is not bound."
        remediation = [D(f"kubectl -n {c.ns} get pvc", "Find the Pending claim and its StorageClass.")]
        status, conf = "probable", 0.7
    elif "Too many pods" in msg:
        cause = "Every node has reached its maximum pod count (max-pods / IP addresses for VPC CNI)."
        remediation = [R("# Add nodes, or enable prefix delegation in the VPC CNI to raise max pods per node",
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
        remediation=[R(ask_default("CPU", f"CPU request per pod for {c.dep_name} (right-sized)", "100m")
                       + f' && kubectl -n {c.ns} set resources deployment/{c.dep_name} --requests=cpu="$CPU"',
                       "Right-size the new workload first (FinOps)."),
                     R("# Or raise the namespace quota through the platform's change process (budget owner approval)",
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
           D(f"aws autoscaling describe-scaling-activities --max-items 5 --auto-scaling-group-name "
             f"\"$(aws eks describe-nodegroup --cluster-name {c.alert.cluster} --nodegroup-name "
             f"{(node.get('labels') or {}).get('eks.amazonaws.com/nodegroup', 'workers')} "
             "--query 'nodegroup.resources.autoScalingGroups[0].name' --output text)\"",
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
                         R("# Then fix the offending workload's limits (memory / ephemeral-storage)", "Prevents recurrence.")],
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
                        D(f"aws eks describe-addon --cluster-name {c.alert.cluster} --addon-name coredns",
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
        remediation=[R(f"kubectl -n {c.ns} set resources deployment/{c.dep_name} -c {ct['name']} "
                       f"--limits=cpu={fmt_cpu(2 * (cpu_milli(lim) or 500))}",
                       "Raise (or remove, per platform policy) the CPU limit based on measured peaks. "
                       + (GITOPS_NOTE if c.gitops else ""))],
        verification=c.verify_rollout(), rollback=[c.undo()],
        expected_outcome="The throttling ratio drops below the alert threshold and latency improves.",
        risk="Low; higher limits let the pod use more node CPU.")


CHECKERS: list[Callable[[Ctx], Finding | None]] = [
    check_certificate, check_coredns, check_node, check_config_error, check_image_pull, check_oom, check_liveness, check_crashloop,
    check_pvc, check_quota, check_pending, check_readiness, check_service_endpoints, check_throttling,
]


def _fill(cmd: str, c: Ctx) -> str:
    for placeholder, value in (("<namespace>", c.alert.namespace), ("<pod>", c.alert.pod),
                               ("<node>", c.alert.node), ("<app>", c.alert.deployment or c.alert.workload)):
        if value:
            cmd = cmd.replace(placeholder, value)
    return cmd


def inconclusive(c: Ctx) -> Finding:
    """No checker explains the evidence. Use the alert's runbook if there is one; never invent a cause."""
    rb = runbooks.lookup(c.alert.alertname)
    evidence: list[Evidence] = []
    if rb:
        text = c.alert.description or c.alert.summary or rb.meaning
        evidence.append(Evidence(source="alert", observation=f"{c.alert.alertname} is firing: {text}"))
    for p in c.ev.pods[:2]:
        evidence.append(Evidence(source="pod.status", observation=f"pod {p['name']} phase {p.get('phase')}, containers "
                                 + ", ".join(f"{x['name']}={x['state']} ready={x.get('ready')} restarts="
                                             f"{x.get('restart_count')}" for x in p.get("containers", []))))
    if c.ev.deployment:
        evidence.append(Evidence(source="deployment.status", observation=str(c.ev.deployment.get("status"))))
    warnings = [e for e in c.ev.events if e.get("type") == "Warning"]
    evidence += [c.ev_event(e) for e in warnings[-3:]]
    generic_factors = ["The condition may have been transient and already cleared (hypothesis)",
                       "The cause may be outside Kubernetes (a dependency, the network, the cloud provider) (hypothesis)"]
    runbook_checks = [D(_fill(cmd, c), why) for cmd, why in rb.checks] if rb else []
    return Finding(
        rb.category if rb else "unknown",
        (f"{rb.meaning} ({c.alert.summary})" if rb and c.alert.summary else rb.meaning) if rb else
        (c.alert.summary or f"Alert {c.alert.alertname} fired; the collected evidence shows no known failure pattern."),
        INCONCLUSIVE, "inconclusive", 0.2 if evidence else 0.0, evidence,
        factors=([f"{cause} (hypothesis)" for cause in rb.likely_causes] if rb else []) + generic_factors,
        investigations=runbook_checks + [
            D(f"kubectl -n {c.ns} get events --sort-by=.lastTimestamp | tail -30", "Recent events in the namespace."),
            D(f"kubectl -n {c.ns} get pods -o wide", "Current pod status."),
            D("# Open the Grafana dashboards for the namespace/pod around the alert time",
              "Metrics history shows what changed (traffic, latency, memory)."),
            D("# Check the Argo CD history and Git log for changes just before the alert", "Correlate with releases.")]
        + ([c.describe_pod()] if c.pod else []),
        expected_outcome="Further evidence identifies the cause, or the alert clears on its own.",
        risk="None from investigating; do not apply changes until the cause is known.")


# ---------------------------------------------------------------------- report assembly

def business_impact(c: Ctx, finding: Finding) -> str:
    if finding.incident_type in ("CertificateExpired", "TLSHostnameMismatch", "TLSUntrustedCertificate"):
        users = ", ".join(c.ev.secret_consumers) or (c.ev.tls or {}).get("endpoint") or "the endpoint"
        return (f"Now: every client that verifies TLS (browsers, most services, partner integrations) fails to "
                f"connect to {users}. Clients that skip verification keep working, which can hide the problem.")
    if finding.incident_type == "CertificateExpiringSoon":
        users = ", ".join(c.ev.secret_consumers) or (c.ev.tls or {}).get("endpoint") or "its users"
        return f"No impact yet. At expiry, {users} will be rejected by verifying clients: a hard outage with a known date."
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
                         "incident dashboard and your change process. Lines starting with # are instructions; "
                         "replace any value in <angle brackets> before running. "
                         + (GITOPS_NOTE if c.gitops else "")).strip(),
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
