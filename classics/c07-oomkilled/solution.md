# c07 - OOMKilled (exit code 137)

```bash
kubectl -n c07-oomkilled describe pod -l app=cache-warmer | grep -A5 "Last State"
#   Last State: Terminated   Reason: OOMKilled   Exit Code: 137
kubectl -n c07-oomkilled set resources deploy/cache-warmer --requests=memory=128Mi --limits=memory=160Mi
kubectl -n c07-oomkilled logs deploy/cache-warmer         # cache warmed: 96 MB
kubectl top pod -n c07-oomkilled                          # if metrics-server is installed
```

**Takeaways**
- The kernel kills the container when it exceeds its **memory limit** (cgroup), regardless of free
  node memory. Exit code 137 = 128 + SIGKILL(9).
- Distinguish a *fixed* need (raise the limit - this case) from a *leak* (memory grows forever:
  raising the limit only delays it - roll back and fix the code).
- Set memory request = what it really uses, limit = request + headroom. JVM/Python/Node runtimes
  need headroom on top of the data; for the JVM use `-XX:MaxRAMPercentage`.
