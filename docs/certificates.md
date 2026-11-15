# Certificate monitoring and incidents

Certificate problems cause hard outages, often on a date known months in advance. This adds two
independent checks whose alerts go to the SRE agent, which explains each one in an incident email and an RCA.

```
every kubernetes.io/tls Secret ──► x509-certificate-exporter ──► x509_cert_not_after ─┐
HTTPS endpoints (Probe objects) ──► blackbox exporter ──► probe_success / ssl expiry ─┤
                                                                                       ▼
                                           certificate-rules.yaml ──► Alertmanager ──► SRE agent
                     agent (read-only): TLS handshake like a client + who uses the Secret (pod specs, Ingresses)
                                           ──► incident email ──► RCA for review ──► engineer signs off
```

| Check | Sees | Misses |
|---|---|---|
| **x509-certificate-exporter** | Expiry of every certificate stored in the cluster, served or not | Hostname, chain and trust problems; certificates outside the cluster |
| **blackbox exporter** (strict + reachability) | What clients see: expired, wrong hostname, untrusted issuer, incomplete chain, endpoint down | Endpoints nobody listed in a Probe |
| **Agent TLS check** (per incident) | The exact verification error and the presented certificate (names, issuer, dates) | Nothing new; it explains the alert |

**Access:** the exporter needs read access to Secrets, so it's a separate component with its own RBAC.
The agent still has **no access to Secrets**. It reads the certificate an endpoint presents in a handshake,
which is public, and finds who uses a Secret from pod specs and Ingresses.

## Alerts

| Alert | Fires when | Agent's diagnosis |
|---|---|---|
| CertificateExpiringSoon | < 30 days left (15 min) | CertificateExpiringSoon: renew before the date |
| CertificateExpiringCritical | < 3 days left (5 min) | CertificateExpiringSoon, critical |
| CertificateExpired | expired | CertificateExpired: outage for verifying clients |
| TLSEndpointHandshakeFailing | reachable, but a verifying client rejects it (3 min) | TLSHostnameMismatch / CertificateExpired / TLSUntrustedCertificate, from the real handshake error |
| TLSEndpointCertExpiringSoon | the served certificate has < 30 days (15 min) | CertificateExpiringSoon (same incident as the Secret's) |
| TLSEndpointDown | no TLS handshake (5 min) | TLSEndpointUnreachable |
| CertificateExporterReadErrors | the exporter can't parse some certificates | runbook |

Each RCA names the certificate (CN, DNS names, issuer, expiry), its consumers, and a resolution guide.
The guide covers reissuing with the right DNS names, replacing the Secret, restarting the consumer, verifying
with `openssl s_client`, and rollback. Corrective and preventive actions are added: automate renewal with
cert-manager or ACM using renewBefore of 30 days or more, keep a certificate inventory with owners, alert at
30 / 14 / 3 days, and probe every endpoint.

## Install (jump host, after make monitoring and make sre-agent-up)

```bash
cd ~/CACIB && git pull
make sre-agent-up SMTP_USER=... EMAIL_TO=... DASHBOARD_IP=<laptop IP>   # new image: TLS checks, Ingress read
make cert-monitoring
```
`make cert-monitoring` creates a **lab internal CA** in `~/.cert-lab` (its key never leaves the jump host)
and stores its public certificate as `tls-trust-bundle`. It then has Argo CD install both exporters, with
the chart versions pinned at install, and adds the alert rules. At work, pass the bank's root CA instead:
`TRUST_BUNDLE=/path/to/bank-root.pem make cert-monitoring`.

**To monitor your own endpoints,** add a Probe per endpoint, as `scripts/cert-lab.sh` does. Use job
`tls-strict` (module `tls_strict`) and `tls-reachability` (module `tls_reachability`), with labels
`target_namespace`, `deployment` and `secret`, so both sources join into one incident.

## Test it

```bash
make cert-lab ARGS=start     # web-healthy (control), web-expiring (1 day), web-wronghost (wrong names)
make cert-lab ARGS=status    # pods, certificates as stored, firing certificate alerts
```
After about 6 minutes you get **two incident emails**:
- **web-expiring:** CertificateExpiringSoon, critical. The certificate expires in 1 day; the email shows who
  uses the Secret and how to renew it.
- **web-wronghost:** TLSHostnameMismatch. Clients connect to `web-wronghost.cert-lab.svc`, but the certificate
  is for `payments.wrong.example` and `api.wrong.example`.

`web-healthy` raises nothing, which proves the checks aren't simply always red.

Then do what the guide says, or run `make cert-lab ARGS=renew`. Both certificates are reissued correctly and
the servers restart. The alerts resolve within about 5 minutes, and the **RCA for review** emails arrive.
Complete and sign off each RCA in the dashboard. Clean up with `make cert-lab ARGS=reset`.

## Not verified from the build environment

Verified offline: the agent's TLS check against real TLS servers (valid, wrong host, expiring, expired,
untrusted CA), the diagnosis and RCA for each case, and the lab's `openssl` certificates.

Not verified until the first live run: the two Helm charts' value keys, the exporter's metric labels
(`secret_namespace`, `secret_name`, `subject_CN`) and the Probe job labels. If an alert doesn't fire,
check `make alert-coverage` and Prometheus **Status > Targets** for the `tls-*` and x509 jobs.
