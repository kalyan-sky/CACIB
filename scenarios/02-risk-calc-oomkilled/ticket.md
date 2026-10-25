**Short description:** Intraday market-risk (VaR) figures have not refreshed for 40 minutes
**Reported by:** Market Risk Management | **Assignment group:** Platform Engineering - Container Platform
**Affected service:** `treasury-markets/risk-calc-engine` (tier-2)

Traders are running against stale risk limits. The treasury team says the
"pods look Running most of the time" but the RESTARTS counter keeps climbing.
They believe the platform is unstable and want the nodes checked.

**Recent changes**
- Release v2.3.1 of risk-calc-engine went out through the CD pipeline about
  30 minutes ago ("enable eager VaR cache"). The pipeline reported success.

**Acceptance criteria**
1. Determine *why* the container restarts (platform fault or application fault?), with evidence.
2. Restore a stable risk-calc-engine: all replicas available, not restarting.
3. Do not just raise the memory limit and walk away - explain in the incident note why that would not fix it.
