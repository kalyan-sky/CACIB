**Reported by:** Payments team · **Priority:** P3

"The platform team gave us a **dedicated node** (`{{NODE}}`, label `team=payments`).
Our `payments-worker` targets it, but it stays Pending."

The node's taint is intentional - it keeps other teams' pods off it. Don't remove it.

**Done when:** `payments-worker` runs on the dedicated node, and the node keeps its taint.

> This case taints a real node. Run `./k8s-classics.sh reset c06` when you finish,
> or other pods may not schedule on a single-node cluster.
