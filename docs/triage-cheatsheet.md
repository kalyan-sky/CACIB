# Triage cheat-sheet (by symptom)

Always start wide, then narrow: **customer impact -> service -> pods -> node/cluster**.

```bash
./sim.sh status                                   # what is red, and why (per pod)
kubectl get events -A --sort-by=.lastTimestamp | tail -30
kubectl get deploy -A --no-headers | awk '{split($3,a,"/"); if (a[1]!=a[2]) print}'   # not fully ready
```

| Symptom | First commands | Usual suspects |
|---|---|---|
| `CrashLoopBackOff` | `kubectl logs <pod> --previous`, `describe pod` (Last State, Exit Code) | bad/missing config, failing dependency at boot, bad command |
| `OOMKilled`, exit 137 | `describe pod` (Last State), `kubectl top pod` | limit too low, memory leak, JVM heap > limit |
| `ImagePullBackOff` | `describe pod` (Events) | wrong tag, registry auth (imagePullSecrets), mirror/proxy/CA |
| Pods `Pending` | `describe pod` (Events: FailedScheduling) | insufficient CPU/memory, taints, affinity/spread, PVC |
| Deployment 0/N, **no pods** | `describe rs`, `describe resourcequota` | quota exceeded, PSA rejected, admission webhook |
| Running but `0/1 READY` | `describe pod` (Readiness), `get endpoints` | probe path/port wrong, dependency down, slow start |
| Service returns nothing | `get endpoints <svc>`, `get svc -o wide` vs `get pods --show-labels` | selector/label mismatch, no Ready pods |
| Timeouts between services | `kubectl exec deploy/<caller> -- /txn-service probe <url>`, `get netpol -n <target>` | NetworkPolicy, DNS, wrong port |
| Drain hangs | `get pdb -A` (ALLOWED DISRUPTIONS) | PDB too strict, single replica with PDB |
| Rollout stuck | `rollout status`, `rollout history`, `get rs` | new pods not Ready, image pull, quota |

Useful one-liners:

```bash
kubectl -n <ns> rollout history deploy/<app>          # what changed (CHANGE-CAUSE)
kubectl -n <ns> rollout undo deploy/<app>             # fastest safe restore
kubectl diff -f rendered/<ns>/<app>.yaml              # drift vs git
kubectl -n <ns> get deploy <app> -o jsonpath='{.spec.template.spec.containers[0].env}'
kubectl get pods -A --field-selector=status.phase!=Running
kubectl get pods -A -o wide --field-selector spec.nodeName=<node>
kubectl auth can-i --list -n payments --as=x --as-group=team-payments
python3 tools/platform.py audit --live -n <ns>
python3 tools/platform.py capacity --live
```

Incident note template:

```
Summary:        what customers saw, from when to when
Impact:         services, business lines, volume (payments, clients); regulatory notification needed?
Timeline:       detection -> triage -> mitigation -> resolution (UTC+8)
Root cause:     the technical cause and the process gap that allowed it
Resolution:     exactly what was changed, by whom, how it was verified
Preventive:     owner + due date for each action (guardrail, automation, monitoring, runbook)
```
