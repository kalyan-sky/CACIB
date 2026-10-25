# RITM0072301 - reference solution

Add to `inventory/applications.yaml`:
```yaml
- name: fx-options-pricer
  domain: treasury-markets
  tier: 2
  description: Black-Scholes / Garman-Kohlhagen FX option pricing
  upstreams: [treasury-markets/fx-rates]
  resources: {cpu: 50m, memory: 64Mi}
```
and change the portal's entry:
```yaml
- name: corporate-portal-api
  upstreams: [shared-services/auth-token-service, core-banking/account-service, treasury-markets/fx-options-pricer]
```
Then:
```bash
make render
kubectl diff -R -f rendered/ | less     # preview: new app, new NetworkPolicy rule, portal ConfigMap + rollout
make apply
kubectl -n treasury-markets rollout status deploy/fx-options-pricer
kubectl -n channels rollout status deploy/corporate-portal-api
python3 tools/platform.py audit --live -n treasury-markets
```

## What the golden path did for you
- Deployment with probes, a hardened securityContext, ownership labels and limits (2x requests).
- ConfigMap with `UPSTREAMS`, plus a `config-hash` on the portal's pod template,
  so the portal **rolled automatically** to pick up the new upstream.
- `allow-from-consumers` in `treasury-markets` now includes `channels`. It was
  derived from the dependency graph, nobody wrote it by hand.
- The quota for `treasury-markets` grew with the new app's requests.

At 200+ apps this is the job: make the right way the easy way, so app teams
get a compliant deployment by filling in ten lines, not three hundred.
In the real world the same idea is a Helm library chart, Kustomize bases,
Backstage templates, or a platform API / operator.
