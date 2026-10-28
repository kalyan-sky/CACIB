1. Many apps, same symptom, no app change -> look for a **shared** dependency. For names, that's cluster DNS.
2. `kubectl -n kube-system get deploy,pods -l k8s-app=kube-dns`
3. `kubectl -n kube-system get endpoints kube-dns` - does the DNS Service have endpoints?
4. Restore the normal replica count (2 on most clusters).
