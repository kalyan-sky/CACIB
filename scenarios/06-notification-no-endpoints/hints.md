1. `kubectl -n shared-services get pods -l app.kubernetes.io/name=notification-service` - look at the READY column, not STATUS.
2. `kubectl -n shared-services get endpoints notification-service` (or `get endpointslices`).
3. `kubectl -n shared-services describe pod <pod>` - what is the readiness probe hitting, and what does the app listen on?
4. `kubectl -n shared-services get deploy notification-service -o jsonpath='{.spec.strategy}'` - why were the old pods gone before the new ones were Ready?
