# Classic Kubernetes debugging cases

Sixteen short, self-contained cases. These are the incidents every Kubernetes engineer meets,
and every interviewer asks about. They run on **any** cluster, using small public images
(nginx, busybox, python, curl). Each case lives in its own namespace.

```bash
./k8s-classics.sh list              # cases + your progress
./k8s-classics.sh start c01         # break it, print the ticket
./k8s-classics.sh hint c01          # stuck? hints from gentle to explicit
./k8s-classics.sh check c01         # verify your fix (records your time)
./k8s-classics.sh solution c01      # reference fix + takeaways
./k8s-classics.sh reset c01         # clean up (always do this for c06 and c16)
./k8s-classics.sh page              # a random unsolved case: no peeking at the list
```

| ID | Level | You'll see | Classic cause |
|---|---|---|---|
| c01 | easy | CrashLoopBackOff | invalid config in a mounted ConfigMap |
| c02 | easy | ImagePullBackOff | image tag typo |
| c03 | easy | CreateContainerConfigError | Secret key doesn't exist |
| c04 | easy | Pending: Insufficient cpu | request bigger than any node |
| c05 | easy | Pending: node selector | nodeSelector matches no node |
| c06 | medium | Pending: untolerated taint | dedicated node, missing toleration (**touches a node**) |
| c07 | medium | OOMKilled, exit 137 | memory limit below the real need |
| c08 | medium | Running 0/1, no endpoints | readiness probe on a missing path |
| c09 | medium | Service has no endpoints | selector/label mismatch |
| c10 | medium | endpoints OK, connection refused | wrong `targetPort` |
| c11 | medium | `bad address` | short DNS name across namespaces |
| c12 | medium | HTTP 403 from the API | missing Role/RoleBinding (least privilege is checked) |
| c13 | medium | restarts every few seconds | liveness probe kills a slow starter |
| c14 | medium | pod + PVC Pending | StorageClass doesn't exist |
| c15 | medium | Init:0/1 | init container waits for the wrong Service |
| c16 | hard | every lookup fails | CoreDNS down (**cluster-wide**) |

## Where to run them (free options first)

**Killercoda (free, in the browser, nothing to install).** Open
<https://killercoda.com/playgrounds/scenario/kubernetes>, sign in, and in its terminal run:
```bash
git clone https://gitlab.com/kalyansiddam0/cacib.git && cd cacib
./k8s-classics.sh list
```
That gives you a real 2-node kubeadm cluster for about an hour per session; start a new one any time.

**kind on your laptop** (Docker Desktop): `kind create cluster`, then run the same commands.

**EKS:** works too (`terraform apply` + `aws eks update-kubeconfig`), but costs credits. Not needed for these cases.

Needs: `bash`, `kubectl` pointed at the cluster, and internet access for the images.

## How to practise

1. Read the ticket, then **before any hint** write down: the symptom, which layer it is
   (scheduling? image? config? network? DNS? permissions?), and your first command.
2. Investigate with the three questions that solve most cases:
   - `kubectl get` (STATUS / READY / RESTARTS)
   - `kubectl describe` (Events at the bottom)
   - `kubectl logs` (add `--previous` for crashes, `-c <name>` for init containers)
3. Fix with the smallest correct change. Then `check`, then read `solution`: the takeaways are interview answers.
4. Redo each case until you can solve it in **under 5 minutes** without hints.

The symptom-to-cause cheat-sheet is in [`../docs/triage-cheatsheet.md`](../docs/triage-cheatsheet.md).
