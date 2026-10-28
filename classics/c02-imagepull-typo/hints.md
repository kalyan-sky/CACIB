1. `kubectl -n {{NS}} get pods` - STATUS column.
2. No app logs means the container never started. Look at the pod's **Events**: `kubectl -n {{NS}} describe pod -l app=api | tail -15`
3. Read the image name in the event very carefully.
4. `kubectl -n {{NS}} set image deploy/api api=<correct image>`
