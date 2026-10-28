1. `kubectl -n {{NS}} get pods` - this status is not a crash: the container was never created.
2. `kubectl -n {{NS}} describe pod -l app=billing | tail` - which key does it complain about?
3. Compare with what the Secret really contains: `kubectl -n {{NS}} get secret db-credentials -o jsonpath='{.data}'`
4. Fix either side (the Secret key, or the reference) - which one is safer to change?
