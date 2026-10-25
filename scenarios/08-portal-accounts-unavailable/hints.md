1. `./sim.sh status` - which flow is DOWN, and what is the last error?
2. Pods healthy but callers failing: look at what sits between them - `kubectl -n core-banking get svc,endpoints account-service`
3. `kubectl -n core-banking get svc account-service -o wide` vs `kubectl -n core-banking get pods --show-labels | grep account`
