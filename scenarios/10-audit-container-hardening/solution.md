# AUD-2026-117 - reference solution

## Evidence
```bash
python3 tools/platform.py audit --live | tee audit-evidence-$(date +%F).txt
#   [C03] data-reporting/deployment/mas-610-reporting: container app: privileged: true
#   [C03] ... runs as UID 0, root filesystem writable, allowPrivilegeEscalation not false ...
#   [C01] ... missing limits.cpu / limits.memory
#   [C04] ... image bank-sim/txn-service:latest not pinned to a version
#   [C07] data-reporting/namespace/data-reporting: Pod Security Admission not enforcing 'restricted'
```

## Remediation
`kubectl apply -f rendered/...` fails here:
`allowPrivilegeEscalation: false` and `privileged: true` together are invalid.
Client-side apply merges and keeps `privileged: true`, because it was set by a
`kubectl patch`, not by apply. Replace the object with the approved version:
```bash
kubectl diff -f rendered/data-reporting/mas-610-reporting.yaml
kubectl replace -f rendered/data-reporting/mas-610-reporting.yaml
kubectl -n data-reporting rollout status deploy/mas-610-reporting

kubectl label --dry-run=server --overwrite ns data-reporting pod-security.kubernetes.io/enforce=restricted
kubectl label --overwrite ns data-reporting pod-security.kubernetes.io/enforce=restricted
python3 tools/platform.py audit --live -n data-reporting      # no findings
```
If the job really needs a writable `/tmp`, give it an `emptyDir` mounted at
`/tmp`. That needs no root and no writable root filesystem.

Note the LimitRange quietly injected default limits into the *pods*, but the
*Deployment* no longer declared any. Always audit the source of truth, not
only what you see running.

## Management response (example)
- **Root cause:** a vendor workaround was applied directly in production with
  `kubectl`. The PSA guardrail was relaxed to allow it. The change bypassed
  change management and was never reverted.
- **Remediation:** the workload was restored to the hardened baseline and PSA
  `restricted` re-enforced on <date>. Evidence attached.
- **Prevention:** (1) remove `patch namespaces` from application teams and
  break-glass accounts outside PAM; (2) an admission policy (Kyverno/Gatekeeper)
  that denies changes to `pod-security.kubernetes.io/*` labels without an
  exception record; (3) a scheduled `audit --live` in CI with alerts to the
  platform team; (4) GitOps drift detection.

## Talking points
MAS TRM (secure configuration, privileged access management, change
management) · Pod Security Standards · CIS Kubernetes Benchmark · kube-bench ·
policy-as-code · evidence for auditors · client-side vs server-side apply vs replace.
