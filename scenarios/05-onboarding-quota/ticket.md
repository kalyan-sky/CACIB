**Short description:** Go-live of `trade-docs-ocr` (trade document OCR) - deployment shows 0/2 Ready
**Requested by:** Trade Finance application team | **Assignment group:** Platform Engineering - Container Platform

The team deployed their new service to `trade-finance` with their own
manifest. The pipeline is green, but `kubectl get deploy` shows **0/2** and no
pods exist at all. They ask the platform team to "just give us more quota".

**Information from the team**
- Load test results (p95 per pod): **80m CPU, 110Mi memory**.
- They copied the resource block from a Java service: requests 750m / 768Mi.

**Policy (FinOps / capacity management)**
- Requests should sit within ~1.5x of the measured p95, and at most 250m CPU / 256Mi per pod
  without a capacity review.
- Quota increases need a CAB-approved capacity review (two-week lead time).

**Acceptance criteria**
1. `trade-docs-ocr` runs with 2/2 replicas available.
2. Requests per container <= 250m CPU and <= 256Mi memory.
3. The `trade-finance` ResourceQuota is **not** increased.
