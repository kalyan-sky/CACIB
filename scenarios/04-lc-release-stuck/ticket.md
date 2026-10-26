**Short description:** CD pipeline "lc-issuance 1.5.1" failed: `kubectl rollout status` timed out after 10 minutes
**Reported by:** Release Management | **Assignment group:** Platform Engineering - Container Platform
**Affected service:** `trade-finance/lc-issuance` (tier-2)

The trade-finance team's release failed in the deployment stage. The release
manager asks:

1. Are customers affected **right now**? (Trade Ops must be told before the 16:00 SWIFT MT700 batch.)
2. Why did the rollout not complete?
3. Leave the deployment in a clean, fully rolled-out state on the last good
   version. The team will re-release tomorrow.

**Acceptance criteria**
1. `lc-issuance` rollout complete: all replicas updated and available.
2. No pods stuck in `ImagePullBackOff` / `ErrImagePull`.
3. In your incident note: was there customer impact, and why or why not?
