1. `kubectl -n {{NS}} describe pod -l app=payments-worker | tail -5`
2. `kubectl describe node <node> | grep -A3 Taints`
3. A taint repels pods unless they **tolerate** it. Add a toleration to the pod template:
   key `dedicated`, operator `Equal`, value `payments`, effect `NoSchedule`.
