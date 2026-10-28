1. `kubectl -n {{NS}} get pod,pvc` then `kubectl -n {{NS}} describe pvc documents | tail -5`
2. `kubectl get storageclass` - which classes exist here? Which one is `(default)`?
3. A PVC's `storageClassName` cannot be changed. Delete the claim and recreate it with a class
   that exists (or omit the field to use the default), then restart the pod.
4. No StorageClass at all in your cluster? See the solution for a manual PersistentVolume.
   On EKS, dynamic volumes need the **EBS CSI driver** (the Terraform setup installs it with a
   default gp3 class, `ebs-csi-default-sc`).
