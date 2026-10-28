1. `kubectl -n {{NS}} describe pod -l app=analytics | tail -5` - read the scheduler's reason.
2. Which labels does it want, and which labels do nodes have? `kubectl get nodes --show-labels`
3. Two valid fixes: remove the requirement, or label a node. Which one does the ticket ask for?
