**Reported by:** Core banking · **Priority:** P2

"`ledger` needs about 25 seconds to load its snapshot before it opens its port.
It never gets there: Kubernetes keeps restarting it. Logs show only the first line."

**Done when:** `ledger` starts and stays up (no restarts), and a hung ledger would still be restarted.
