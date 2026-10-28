1. Pods are Ready, so look at the Service: `kubectl -n {{NS}} get endpoints orders`
2. `kubectl -n {{NS}} get svc orders -o wide` (SELECTOR) vs `kubectl -n {{NS}} get pods --show-labels`
3. Fix the side that is wrong (a Deployment's selector is immutable - which side is easier and correct?).
