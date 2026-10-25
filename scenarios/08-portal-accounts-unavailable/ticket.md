**Short description:** Corporate e-banking portal shows "Accounts unavailable" to every client
**Reported by:** Digital Channels L2 Support | **Assignment group:** Platform Engineering - Container Platform
**Affected services:** `channels/corporate-portal-api` (tier-1), `core-banking/account-service` (tier-1)

All corporate clients see an error on the accounts page. L2 checked:
"all pods are Running and Ready, no restarts, CPU is low - Kubernetes looks fine."

**Recent changes**
- PR #482 merged by core-banking this morning: "refactor: rename account-service labels to account-svc".
  The team says the PR "only touched metadata".

**Acceptance criteria**
1. `corporate-portal-api` -> `account-service` works.
2. An end-to-end portal transaction from the traffic tier succeeds.
