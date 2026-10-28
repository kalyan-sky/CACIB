**Reported by:** Data team · **Priority:** P3

"The new `reports` service has been Pending since it was deployed. The developer
copied the resource settings from the old VM sizing sheet. We measured it at
about 50m CPU in testing."

**Done when:** `reports` runs, with a CPU request sized for the workload (<= 500m).
