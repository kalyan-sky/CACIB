1. `kubectl -n {{NS}} get pods` - `Init:0/1` = the first init container hasn't finished.
2. Init containers have their own logs: `kubectl -n {{NS}} logs deploy/payments-api -c wait-for-db --tail=5`
3. Which name is it waiting for, and which Services exist? `kubectl -n {{NS}} get svc`
