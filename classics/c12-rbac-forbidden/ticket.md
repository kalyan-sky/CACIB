**Reported by:** SRE tooling · **Priority:** P3

"`pod-reporter` lists the pods in its own namespace for a dashboard. It only
gets HTTP 403. A colleague suggested 'just give it cluster-admin'."

The security team requires **least privilege**: it may *read pods in its own namespace* - nothing more.

**Done when:** the reporter gets HTTP 200, and its ServiceAccount cannot delete pods,
read Secrets, or read other namespaces.
