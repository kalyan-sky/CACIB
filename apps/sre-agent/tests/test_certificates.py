"""Certificate incidents: the real TLS probe against a local TLS server, and the RCA for each case."""

from __future__ import annotations

import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fakes import FakeKube, am_alert, deployment, pod
from test_scenarios import commands_of, run

from sre_agent import tlsprobe
from sre_agent.tlsprobe import ProbeNotAllowed

NS = "cert-lab"
NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------- a tiny CA and TLS server

def _cert(subject: str, issuer_key, issuer_name, key, *, sans: list[str], not_before, not_after, ca=False):
    b = (x509.CertificateBuilder()
         .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
         .issuer_name(issuer_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(not_before).not_valid_after(not_after)
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if sans:
        b = b.add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in sans]), critical=False)
    return b.sign(issuer_key, hashes.SHA256())


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Bank Lab Internal CA")])
    ca = _cert("Bank Lab Internal CA", ca_key, ca_name, ca_key, sans=[], ca=True,
               not_before=NOW - timedelta(days=1), not_after=NOW + timedelta(days=3650))
    (d / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))

    def issue(name: str, sans: list[str], days_from: float, days_to: float) -> tuple[str, str]:
        key = ec.generate_private_key(ec.SECP256R1())
        crt = _cert(sans[0] if sans else name, ca_key, ca_name, key, sans=sans,
                    not_before=NOW + timedelta(days=days_from), not_after=NOW + timedelta(days=days_to))
        (d / f"{name}.crt").write_bytes(crt.public_bytes(serialization.Encoding.PEM))
        (d / f"{name}.key").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                          serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        return str(d / f"{name}.crt"), str(d / f"{name}.key")

    return {"ca": str(d / "ca.crt"), "issue": issue}


@pytest.fixture
def tls_server():
    servers = []

    def start(cert: str, key: str) -> int:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        servers.append(sock)

        def serve():
            while True:
                try:
                    conn, _ = sock.accept()
                except OSError:
                    return
                try:
                    with ctx.wrap_socket(conn, server_side=True) as tls:
                        tls.recv(1)
                except (ssl.SSLError, OSError):
                    pass

        threading.Thread(target=serve, daemon=True).start()
        return sock.getsockname()[1]

    yield start
    for s in servers:
        s.close()


def probe(port, pki, ca=True):
    return tlsprobe.probe(f"https://localhost:{port}", allowed_suffixes=("localhost",),
                          ca_file=pki["ca"] if ca else None, timeout=3)


# ---------------------------------------------------------------------- the probe itself

def test_probe_valid_certificate(pki, tls_server):
    r = probe(tls_server(*pki["issue"]("good", ["localhost"], -1, 90)), pki)
    assert r["verified"] and r["hostname_matches"] and r["issuer_cn"] == "Bank Lab Internal CA"
    assert 89 < r["days_left"] <= 90 and r["dns_names"] == ["localhost"]


def test_probe_wrong_hostname(pki, tls_server):
    r = probe(tls_server(*pki["issue"]("wrong", ["payments.wrong.example"], -1, 90)), pki)
    assert not r["verified"] and "hostname mismatch" in r["verify_error"].lower()
    assert r["hostname_matches"] is False and r["dns_names"] == ["payments.wrong.example"]


def test_probe_expiring_and_expired(pki, tls_server):
    soon = probe(tls_server(*pki["issue"]("soon", ["localhost"], -30, 1)), pki)
    assert soon["verified"] and 0.9 < soon["days_left"] <= 1
    gone = probe(tls_server(*pki["issue"]("expired", ["localhost"], -30, -2)), pki)
    assert not gone["verified"] and "expired" in gone["verify_error"] and gone["days_left"] < 0


def test_probe_untrusted_issuer(pki, tls_server):
    r = probe(tls_server(*pki["issue"]("untrusted", ["localhost"], -1, 90)), pki, ca=False)
    assert not r["verified"] and "local issuer" in r["verify_error"]


def test_probe_allowlist_and_unreachable(pki):
    for target in ("https://evil.example.com", "https://10.0.0.1", "http://web.cert-lab.svc"):
        with pytest.raises(ProbeNotAllowed):
            tlsprobe.probe(target)  # default allowlist: in-cluster Service names only
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    r = tlsprobe.probe(f"https://localhost:{free}", allowed_suffixes=("localhost",), timeout=2)
    assert "connect_error" in r and not r["verified"]


# ---------------------------------------------------------------------- the RCA for each case

HOST = "web-wronghost.cert-lab.svc"
POD = "web-wronghost-7d9f8c6b5d-x7k2p"


def tls_result(**kw):
    base = {"endpoint": f"{HOST}:443", "hostname": HOST, "verified": True, "subject_cn": HOST,
            "issuer_cn": "Bank Lab Internal CA", "dns_names": [HOST], "not_after": "2026-12-01T00:00:00+00:00",
            "days_left": 60.0, "hostname_matches": True, "self_signed": False}
    return {**base, **kw}


def cert_pod():
    return pod(POD, "web-wronghost", ns=NS, ready=True, image="nginx:1.27-alpine",
               volumes=[{"name": "tls", "secret": {"secretName": "web-wronghost-tls"}}])


def run_tls(settings, store, tls, **labels):
    fake = FakeKube(pods=[cert_pod()], deployments=[deployment("web-wronghost", ns=NS, available=1, replicas=1)])
    import test_scenarios

    original = test_scenarios.Pipeline

    class P(original):  # inject the TLS result instead of a network call
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.tls_prober = lambda endpoint: tls

    test_scenarios.Pipeline = P
    try:
        return run(settings, store, fake, **labels)
    finally:
        test_scenarios.Pipeline = original


def test_rca_hostname_mismatch(settings, store, smtp):
    tls = tls_result(verified=False, verify_error="Hostname mismatch, certificate is not valid for "
                                                  f"'{HOST}'.", dns_names=["payments.wrong.example"],
                     subject_cn="payments.wrong.example", hostname_matches=False)
    report, inc, _ = run_tls(settings, store, tls, alertname="TLSEndpointHandshakeFailing", severity="warning",
                             namespace=NS, deployment="web-wronghost", instance=f"https://{HOST}:443")
    assert report.incident_type == "TLSHostnameMismatch" and report.root_cause_status == "confirmed"
    assert "payments.wrong.example" in report.root_cause and HOST in report.root_cause
    assert report.severity in ("HIGH", "CRITICAL")
    assert any(e.source == "tls.verify" and "Hostname mismatch" in e.observation for e in report.supporting_evidence)
    assert any("mounts Secret(s) web-wronghost-tls" in e.observation for e in report.supporting_evidence)
    rem = commands_of(report, "remediation")
    assert any(HOST in c for c in rem) and any("create secret tls web-wronghost-tls" in c for c in rem)
    assert any("s_client" in c for c in commands_of(report, "verification"))
    assert any(c.startswith("openssl s_client") for c in inc["evidence"]["commands_executed"])


def test_rca_expired_endpoint(settings, store, smtp):
    tls = tls_result(verified=False, verify_error="certificate has expired", days_left=-2.0,
                     not_after="2026-11-13T00:00:00+00:00")
    report, _, _ = run_tls(settings, store, tls, alertname="TLSEndpointHandshakeFailing", severity="warning",
                           namespace=NS, deployment="web-wronghost", instance=f"https://{HOST}:443")
    assert report.incident_type == "CertificateExpired" and report.severity == "CRITICAL"
    assert "2026-11-13" in report.root_cause


def test_rca_untrusted(settings, store, smtp):
    tls = tls_result(verified=False, verify_error="unable to get local issuer certificate")
    report, _, _ = run_tls(settings, store, tls, alertname="TLSEndpointHandshakeFailing", namespace=NS,
                           deployment="web-wronghost", instance=f"https://{HOST}:443")
    assert report.incident_type == "TLSUntrustedCertificate"
    assert any("fullchain" in c for c in commands_of(report, "remediation"))


def test_rca_expiring_from_exporter_alert_without_endpoint(settings, store, smtp):
    """x509-certificate-exporter read the Secret; the agent finds who uses it without reading it."""
    fake = FakeKube(pods=[cert_pod()], deployments=[deployment("web-wronghost", ns=NS, available=1, replicas=1)])
    report, inc, _ = run(settings, store, fake, alertname="CertificateExpiringCritical", severity="critical",
                         namespace=NS, secret="web-wronghost-tls", subject_CN=HOST)
    assert report.incident_type == "CertificateExpiringSoon" and report.root_cause_status == "confirmed"
    assert f"CN={HOST}" in report.root_cause
    assert inc["evidence"]["secret_consumers"] == ["deployment/web-wronghost"]
    assert "No impact yet" in report.business_impact
    assert any("rollout restart deployment/web-wronghost" in c for c in commands_of(report, "remediation"))


def test_agent_never_reads_the_secret(settings, store, smtp):
    fake = FakeKube(pods=[cert_pod()], deployments=[deployment("web-wronghost", ns=NS, available=1, replicas=1)])
    run(settings, store, fake, alertname="CertificateExpired", namespace=NS, secret="web-wronghost-tls")
    assert all("secret" not in op for op, _ in fake.calls)
    assert {"list_pods", "list_ingresses"} <= {op for op, _ in fake.calls}


def test_cert_rca_has_cert_capa(settings, store, smtp):
    from sre_agent.rca import build_rca

    fake = FakeKube(pods=[cert_pod()], deployments=[deployment("web-wronghost", ns=NS, available=1, replicas=1)])
    report, inc, _ = run(settings, store, fake, alertname="CertificateExpiringSoon", namespace=NS,
                         secret="web-wronghost-tls")
    rca = build_rca(store.get(report.incident_id), [], store.audit_log(report.incident_id), store.rca_notes("x"))
    assert any("cert-manager" in a.action and a.kind == "preventive" for a in rca.capa)
    assert any(a.kind == "detective" and "30 / 14 / 3" in a.action for a in rca.capa)


def test_rca_expired_from_exporter_alert(settings, store, smtp):
    fake = FakeKube(pods=[cert_pod()], deployments=[deployment("web-wronghost", ns=NS, available=1, replicas=1)])
    report, _, _ = run(settings, store, fake, alertname="CertificateExpired", severity="critical", namespace=NS,
                       secret="web-wronghost-tls", subject_CN=HOST)
    assert report.incident_type == "CertificateExpired" and report.severity == "CRITICAL"
    assert "Secret cert-lab/web-wronghost-tls has expired" in report.root_cause
    assert "Now: every client" in report.business_impact
