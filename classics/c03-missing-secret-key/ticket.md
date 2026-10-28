**Reported by:** Billing team · **Priority:** P3

"We moved the database password into a Kubernetes Secret. Now `billing` shows
a strange status and never starts. `kubectl logs` returns an error."

**Done when:** `billing` runs and gets its password from the `db-credentials` Secret.
