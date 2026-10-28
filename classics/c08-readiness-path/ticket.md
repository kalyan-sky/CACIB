**Reported by:** E-commerce team · **Priority:** P2

"`shop` pods are Running and the logs look normal, but calls to the `shop`
Service fail. A `client` pod is there for testing: `kubectl -n {{NS}} exec deploy/client -- wget -qO- -T 3 http://shop`"

**Done when:** the `shop` Service answers.
