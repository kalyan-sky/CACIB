1. `kubectl -n {{NS}} get pods -w` - RESTARTS count and timing.
2. `kubectl -n {{NS}} describe pod -l app=ledger | grep -E "Liveness|Killing|Unhealthy"`
3. How long does the probe wait before it gives up, versus how long the app needs?
4. Best fix: a **startupProbe** that allows ~60 s for start-up; keep the liveness probe for later hangs.
