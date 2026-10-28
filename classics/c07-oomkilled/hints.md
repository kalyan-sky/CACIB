1. `kubectl -n {{NS}} get pods -w` - watch the STATUS column go through a specific word.
2. `kubectl -n {{NS}} describe pod -l app=cache-warmer | grep -A5 "Last State"` - Reason and Exit Code?
3. Compare the data size with the container's memory **limit**.
4. `kubectl -n {{NS}} set resources deploy/cache-warmer --limits=memory=...` (leave headroom for Python itself)
