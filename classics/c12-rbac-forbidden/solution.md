# c12 - RBAC: 403 Forbidden

```bash
NS=c12-rbac-forbidden
kubectl -n $NS logs deploy/pod-reporter --tail=1              # list pods ... -> HTTP 403
kubectl auth can-i list pods -n $NS --as=system:serviceaccount:$NS:reporter      # no
kubectl -n $NS create role pod-reader --verb=get,list,watch --resource=pods
kubectl -n $NS create rolebinding reporter-reads-pods --role=pod-reader --serviceaccount=$NS:reporter
kubectl auth can-i list pods -n $NS --as=system:serviceaccount:$NS:reporter      # yes
kubectl -n $NS logs deploy/pod-reporter --tail=1              # -> HTTP 200 within 10 s
```

**Takeaways**
- Every pod calls the API as its **ServiceAccount**. `kubectl auth can-i ... --as=system:serviceaccount:<ns>:<sa>`
  is the fastest RBAC debugger there is.
- **Role + RoleBinding** = one namespace. **ClusterRole + ClusterRoleBinding** = every namespace.
  A ClusterRole bound with a *RoleBinding* is still limited to that namespace.
- `cluster-admin` "to make it work" is an audit finding. Give the verbs and resources the code
  actually uses.
