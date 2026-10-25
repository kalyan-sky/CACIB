**Short description:** Internal Audit - technology risk review of the container platform
**Raised by:** Internal Audit (IT) | **Owner:** Head of Platform Engineering | **Due:** 5 business days

During the annual technology-risk review (benchmarked against the MAS
Technology Risk Management Guidelines), Internal Audit sampled workloads and
found evidence that at least one production workload in `data-reporting` runs
with elevated privileges. The namespace's Pod Security guardrail also appears
to have been relaxed.

The data-reporting team says a vendor asked for root "temporarily" so that the
MAS 610 report job could write files under `/tmp`.

**Required actions**
1. Run the platform control audit against the **live** estate and record the findings as evidence.
2. Remediate every finding in `data-reporting`. Do not just suppress them.
3. Restore the namespace guardrail (Pod Security Admission `restricted`).
4. Prepare a short management response: root cause, remediation, and how a
   recurrence will be prevented.

**Acceptance criteria**
- `python3 tools/platform.py audit --live -n data-reporting` reports no findings.
- `data-reporting` enforces PSA `restricted` and `mas-610-reporting` is healthy.
