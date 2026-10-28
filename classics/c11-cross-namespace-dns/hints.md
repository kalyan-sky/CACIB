1. `kubectl -n {{NS}} logs deploy/frontend --tail=3` - what kind of error is it?
2. `kubectl -n {{NS}}-data get svc,endpoints` - is the backend fine?
3. A short Service name only resolves inside the **same** namespace. What is the full name?
   `<service>.<namespace>.svc.cluster.local`
4. `kubectl -n {{NS}} set env deploy/frontend BACKEND_URL=...`
