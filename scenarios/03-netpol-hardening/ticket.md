**Short description:** Payments, PayNow, SWIFT and letters of credit failing with upstream timeouts
**Reported by:** Payments Operations + Trade Operations | **Assignment group:** Platform Engineering - Container Platform
**Affected services:** multiple. Errors mention `fraud-screening` and `sanctions-screening`

Several business lines report failures at the same time. All pods show
Running and Ready. The application teams say "nothing changed on our side".

**Recent changes**
- CHG0031902 (Security Engineering, 30 min ago): "Zero-trust hardening of the
  `risk-compliance` namespace - restrict ingress to the internal trust zone".

Security Engineering insists that the hardening must **not** simply be rolled
back to "allow everything". Only the namespaces that legitimately consume
risk-compliance services may reach them.

**Acceptance criteria**
1. payments-gateway -> fraud-screening, swift-connector -> sanctions-screening and
   lc-issuance -> sanctions-screening work again.
2. Default-deny ingress stays in place in `risk-compliance`.
3. Namespaces that do not consume risk-compliance (e.g. `data-reporting`) still cannot reach it.
