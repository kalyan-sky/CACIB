# c14 - PVC Pending: StorageClass does not exist

```bash
NS=c14-pvc-storageclass
kubectl -n $NS describe pvc documents | tail -2
#   storageclass.storage.k8s.io "fast-ssd" not found
kubectl get storageclass                       # e.g. "standard (default)" on kind, "gp2"/"gp3" on EKS
kubectl -n $NS scale deploy docstore --replicas=0   # nothing may use the claim while it is replaced
kubectl -n $NS delete pvc documents
kubectl -n $NS apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: documents}
spec:                                          # no storageClassName -> the cluster's default class
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 1Gi}}
EOF
kubectl -n $NS scale deploy docstore --replicas=1
kubectl -n $NS get pvc,pod -w
```
No default StorageClass (some playgrounds)? Create a manual volume, then use `storageClassName: ""` in the PVC:
```yaml
apiVersion: v1
kind: PersistentVolume
metadata: {name: documents-pv}
spec:
  capacity: {storage: 1Gi}
  accessModes: [ReadWriteOnce]
  storageClassName: ""
  hostPath: {path: /tmp/documents-pv}
```

**Takeaways**
- `Pending` pod + `Pending` PVC: the scheduler waits for the volume. The PVC's events say why:
  unknown class, no provisioner (e.g. EBS CSI driver not installed on EKS), quota, or a zone conflict.
- `WaitForFirstConsumer` classes stay Pending *until a pod uses them* - that's normal.
- On EKS, dynamic EBS volumes need the **EBS CSI driver** add-on and an IAM role for it.
