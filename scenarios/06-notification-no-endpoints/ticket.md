**Short description:** Corporate KYC onboarding returns errors; clients get no confirmation emails
**Reported by:** Client Onboarding Operations | **Assignment group:** Platform Engineering - Container Platform
**Affected services:** `risk-compliance/kyc-onboarding`, `shared-services/notification-service` (tier-2)

New corporate clients cannot complete onboarding. The KYC team says their
service "is up but notification-service is refusing connections". The
shared-services team pushed a "performance tuning" change to
notification-service this morning and says the pods are Running.

**Acceptance criteria**
1. `notification-service` has Ready endpoints and is reachable from `kyc-onboarding`.
2. The rollout strategy is back to the platform standard (RollingUpdate), so a bad probe can never again take the service fully down.
3. In your incident note: why did a *probe* change cause a full outage?
