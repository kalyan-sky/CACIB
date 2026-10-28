1. Run the test - read the exact error (refused? timeout? bad address?).
2. `kubectl -n {{NS}} get endpoints catalog` - which **port** do the endpoints list?
3. Which port does nginx listen on? `kubectl -n {{NS}} get deploy catalog -o jsonpath='{.spec.template.spec.containers[0].ports}'`
4. Service `port` = what clients call; `targetPort` = where the container listens.
