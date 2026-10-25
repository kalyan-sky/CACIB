1. Run the drain yourself (Ctrl-C to stop it) and read which pod it is stuck on.
2. `kubectl get pdb -A` - look at the ALLOWED DISRUPTIONS column.
3. `kubectl -n core-banking get pdb core-ledger -o yaml` - compare minAvailable with the deployment's replicas.
4. What does the platform's golden path render for tier-1 PDBs? `grep -A6 PodDisruptionBudget rendered/core-banking/core-ledger.yaml`
5. Alternative: temporarily scale to 3 replicas. What are the trade-offs?
