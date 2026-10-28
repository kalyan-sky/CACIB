1. `kubectl -n {{NS}} get pods` -> Pending. Pending = not scheduled yet. The scheduler explains why in the events:
   `kubectl -n {{NS}} describe pod -l app=reports | tail -5`
2. What does the pod request, and what can a node offer? `kubectl describe node | grep -A6 Allocatable`
3. `kubectl -n {{NS}} set resources deploy/reports --requests=cpu=100m --limits=cpu=500m`
