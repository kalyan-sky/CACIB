# c03 - CreateContainerConfigError (missing Secret key)

```bash
kubectl -n c03-missing-secret-key describe pod -l app=billing | tail -3
#   Error: couldn't find key password in Secret c03-missing-secret-key/db-credentials
kubectl -n c03-missing-secret-key get secret db-credentials -o jsonpath='{.data}'; echo
#   {"pasword":"...","username":"..."}     <- typo in the key name
kubectl -n c03-missing-secret-key patch secret db-credentials --type merge   -p '{"stringData":{"password":"s3cr3t-demo"}}'
kubectl -n c03-missing-secret-key delete pod -l app=billing      # retried anyway, this is just faster
```

**Takeaways**
- `CreateContainerConfigError` = kubelet could not build the container config: a missing
  ConfigMap/Secret, or a missing key in it. No logs exist because nothing ran.
- Use `optional: true` only for truly optional settings; a missing password *should* stop the app.
- In a bank, secrets come from Vault / AWS Secrets Manager via External Secrets or CSI,
  and the key names are part of the contract - test them in CI.
