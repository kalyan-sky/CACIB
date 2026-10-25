**Short description:** Onboard `fx-options-pricer` to the container platform
**Requested by:** Treasury & Markets IT | **Assignment group:** Platform Engineering - Container Platform

Treasury is launching FX option quotes on the corporate portal. Please onboard
the pricing service. Use the **golden path**: inventory -> render -> apply. No
hand-written YAML.

| Field               | Value                                                  |
|---------------------|--------------------------------------------------------|
| name                | `fx-options-pricer`                                    |
| domain / namespace  | `treasury-markets`                                     |
| tier                | 2                                                      |
| calls (upstreams)   | `treasury-markets/fx-rates`                            |
| called by           | `channels/corporate-portal-api` (add it to the portal's upstreams) |
| resources (request) | 50m CPU, 64Mi memory                                   |
| description         | Black-Scholes / Garman-Kohlhagen FX option pricing     |

**Acceptance criteria**
1. The app is in `inventory/applications.yaml` and deployed in `treasury-markets` (healthy, tier-2 labels).
2. Requests are 50m / 64Mi.
3. `corporate-portal-api` can reach it (NetworkPolicy updated by the render, not by hand)
   and portal transactions now flow through it.
4. `python3 tools/platform.py audit --live -n treasury-markets` passes.
