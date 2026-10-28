**Reported by:** Catalog team · **Priority:** P2

"`catalog` pods are Ready and the Service has endpoints - but every call fails.
Test: `kubectl -n {{NS}} exec deploy/client -- wget -qO- -T 3 http://catalog`"

**Done when:** `http://catalog` answers from the client.
