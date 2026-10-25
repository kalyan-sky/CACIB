**Short description:** Outgoing corporate payments failing - "Service temporarily unavailable"
**Reported by:** Payments Operations (SG) | **Assignment group:** Platform Engineering - Container Platform
**Affected service:** `payments/payments-gateway` (tier-1)

Since this morning every payment submitted through the corporate portal and
host-to-host channel is rejected. Payments Ops estimates ~1,400 payments queued,
including MEPS+ high-value payments with a 17:00 cut-off.

The incident manager has declared a **major incident**. A notification to MAS
may be required within one hour of discovery, so they need from you within 15
minutes: (1) what is broken, (2) when service will be restored.

**Recent changes (from the change calendar)**
- CHG0031877 (last night, payments team): "Tune DB connection pool settings
  for payments-gateway" - executed manually with `kubectl edit`.
- Overnight: routine node maintenance recycled pods across the cluster.

**Acceptance criteria**
1. `payments-gateway` fully available (all replicas Ready).
2. Its configuration matches the approved version in git (the inventory / `rendered/`).
3. An end-to-end payment from the traffic tier succeeds.
