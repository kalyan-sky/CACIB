1. No pods at all means the problem is *before* scheduling. Who creates pods for a Deployment?
2. `kubectl -n trade-finance describe rs -l app.kubernetes.io/name=trade-docs-ocr` - read the events.
3. `kubectl -n trade-finance describe resourcequota compute-quota` - used vs hard.
4. Right-size the requests based on the load test: `kubectl set resources ...`
5. Stretch: persist it the golden-path way - add the app to `inventory/applications.yaml` with a `resources:` override.
