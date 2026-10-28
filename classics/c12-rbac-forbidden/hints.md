1. `kubectl -n {{NS}} logs deploy/pod-reporter --tail=2`
2. Which identity does the pod use? `kubectl -n {{NS}} get deploy pod-reporter -o jsonpath='{.spec.template.spec.serviceAccountName}'`
3. Test permissions without running anything: `kubectl auth can-i list pods -n {{NS}} --as=system:serviceaccount:{{NS}}:reporter`
4. `kubectl -n {{NS}} create role ... --verb=get,list --resource=pods` and `kubectl -n {{NS}} create rolebinding ... --serviceaccount={{NS}}:reporter`
