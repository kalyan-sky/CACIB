1. `kubectl -n {{NS}} get pods` - what is the STATUS and the RESTARTS count?
2. A crashing container's output is in the *previous* attempt: `kubectl -n {{NS}} logs deploy/web --previous`
3. nginx tells you the file and line. Where does that file come from? `kubectl -n {{NS}} get deploy web -o yaml | grep -A4 volumes`
4. `kubectl -n {{NS}} edit configmap web-config` - count the braces.
5. Pods read a mounted ConfigMap at start-up; restart them after fixing it.
