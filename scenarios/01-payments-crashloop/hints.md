1. Start wide: `./sim.sh status`, then `kubectl -n payments get pods`. What state are the pods in?
2. A crashing container's logs are gone once it restarts - use `kubectl -n payments logs <pod> --previous`.
3. The log names a missing key. Where does the container get its environment from?
   `kubectl -n payments get deploy payments-gateway -o yaml | grep -A3 envFrom`
4. What is the *approved* value? Git is the source of truth:
   `kubectl diff -f rendered/payments/payments-gateway.yaml`
5. Fixing the ConfigMap does not restart pods by itself - how will they pick it up?
