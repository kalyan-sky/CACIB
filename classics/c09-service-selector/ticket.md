**Reported by:** Checkout team · **Priority:** P1

"Someone 'tidied up' the `orders` Service YAML yesterday. Both `orders` pods are
Running and Ready, but nothing can reach the Service. Test with:
`kubectl -n {{NS}} exec deploy/client -- wget -qO- -T 3 http://orders`"

**Done when:** `http://orders` answers from the client.
