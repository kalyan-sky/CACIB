1. Copy an existing entry in `inventory/applications.yaml` and adapt it. Look at the header comment for the fields.
2. To call it from the portal, edit `corporate-portal-api`'s `upstreams` too.
3. `make render` then `git diff --stat` is not possible (rendered/ is generated) - use `kubectl diff -R -f rendered/` to preview.
4. `make apply` (or `kubectl apply -R -f rendered/`).
5. Did the portal pods pick up their new UPSTREAMS? Look at the `bank.sim/config-hash` annotation.
