1. `python3 tools/platform.py audit --live` - evidence first.
2. `kubectl get ns data-reporting --show-labels`
3. `kubectl apply -f rendered/data-reporting/mas-610-reporting.yaml` - does it remove every finding? Why or why not? Try `kubectl diff` first.
4. `kubectl apply` only removes fields that *it* set. Fields added by someone else's `kubectl patch` survive. Consider `kubectl replace`.
5. Before re-enforcing PSA: `kubectl label --dry-run=server --overwrite ns data-reporting pod-security.kubernetes.io/enforce=restricted` warns about violating pods.
6. Writable `/tmp` without root: an `emptyDir` volume mounted at `/tmp`.
