"""Read-only TLS check of an endpoint: what certificate does it present, and does it verify?

This is what any client sees in a handshake: public information only. It needs
no Kubernetes permissions and never reads Secrets. Only hosts on the allowlist
(in-cluster Service names by default) are contacted, so an alert label cannot
make the agent connect to arbitrary addresses.
"""

from __future__ import annotations

import socket
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography import x509
from cryptography.x509.oid import ExtensionOID, NameOID

DEFAULT_SUFFIXES = (".svc", ".svc.cluster.local")


class ProbeNotAllowed(ValueError):
    pass


def target_of(url_or_host: str) -> tuple[str, int]:
    raw = url_or_host if "://" in url_or_host else f"https://{url_or_host}"
    u = urlparse(raw)
    if u.scheme != "https" or not u.hostname:
        raise ProbeNotAllowed(f"not an https endpoint: {url_or_host!r}")
    return u.hostname.lower(), u.port or 443


def _name(cert: x509.Certificate, which: str) -> str:
    attrs = (cert.subject if which == "subject" else cert.issuer).get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(attrs[0].value) if attrs else (cert.subject if which == "subject" else cert.issuer).rfc4514_string()


def probe(url_or_host: str, *, allowed_suffixes: tuple[str, ...] = DEFAULT_SUFFIXES, ca_file: str | None = None,
          timeout: float = 5.0) -> dict[str, Any]:
    host, port = target_of(url_or_host)
    if not any(host == s.lstrip(".") or host.endswith(s) for s in allowed_suffixes):
        raise ProbeNotAllowed(f"{host} is not on the TLS probe allowlist {allowed_suffixes}")
    result: dict[str, Any] = {"endpoint": f"{host}:{port}", "hostname": host, "verified": False}

    # 1. A normal, verifying handshake: the error is exactly what a client would get.
    ctx = ssl.create_default_context()
    # Python 3.13 adds X.509 strict checks that browsers, curl and OpenSSL clients don't apply;
    # judge the certificate as typical clients do.
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    if ca_file and Path(ca_file).exists():
        ctx.load_verify_locations(cafile=ca_file)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock, \
                ctx.wrap_socket(sock, server_hostname=host):
            result["verified"] = True
    except ssl.SSLCertVerificationError as e:
        result["verify_error"] = e.verify_message or str(e)
    except (ssl.SSLError, OSError) as e:
        result["connect_error"] = f"{type(e).__name__}: {e}"
        return result

    # 2. Without verification, to read the certificate that is actually presented.
    raw = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    raw.check_hostname = False
    raw.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock, \
            raw.wrap_socket(sock, server_hostname=host) as tls:
        der = tls.getpeercert(binary_form=True)
        result["tls_version"] = tls.version()
    cert = x509.load_der_x509_certificate(der)
    try:
        sans = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value \
            .get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        sans = []
    not_after = cert.not_valid_after_utc
    result.update({
        "subject_cn": _name(cert, "subject"),
        "issuer_cn": _name(cert, "issuer"),
        "self_signed": cert.issuer == cert.subject,
        "dns_names": sans,
        "not_before": cert.not_valid_before_utc.isoformat(),
        "not_after": not_after.isoformat(),
        "days_left": round((not_after - datetime.now(timezone.utc)).total_seconds() / 86400, 2),
        "serial": format(cert.serial_number, "x"),
        "hostname_matches": any(_matches(host, n) for n in sans),
    })
    return result


def _matches(host: str, pattern: str) -> bool:
    pattern = pattern.lower()
    if pattern.startswith("*."):
        return host.count(".") == pattern.count(".") and host.endswith(pattern[1:])
    return host == pattern
