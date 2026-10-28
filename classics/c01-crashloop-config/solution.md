# c01 - CrashLoopBackOff from a bad config file

```bash
kubectl -n c01-crashloop-config get pods                     # CrashLoopBackOff, RESTARTS climbing
kubectl -n c01-crashloop-config logs deploy/web --previous
#   nginx: [emerg] unexpected end of file, expecting "}" in /etc/nginx/conf.d/default.conf:10
kubectl -n c01-crashloop-config edit configmap web-config    # add the missing closing "}" of server { }
kubectl -n c01-crashloop-config rollout restart deploy/web
kubectl -n c01-crashloop-config rollout status deploy/web
```

**Takeaways**
- CrashLoopBackOff = the container starts and exits, again and again, with growing back-off
  (10 s, 20 s, 40 s ... 5 min). The cause is almost always in `logs --previous` or in
  `describe pod` -> *Last State: Terminated, Exit Code*.
- Exit code 1 = the app failed (config, missing dependency); 137 = killed (OOM / liveness);
  126/127 = wrong command / not found.
- Validate configs before shipping: `nginx -t` in CI, or a ConfigMap generator with a hash
  so a bad config becomes a failed rollout instead of an outage.
