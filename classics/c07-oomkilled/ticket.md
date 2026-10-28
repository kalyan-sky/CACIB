**Reported by:** Reference-data team · **Priority:** P3

"`cache-warmer` loads a fixed 96 MB reference-data set at start-up. It never
finishes - it restarts a few seconds after starting. The logs just stop."

**Done when:** `cache-warmer` runs without restarting, with a memory limit sized for its data.
