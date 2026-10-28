**Reported by:** Several teams at once · **Priority:** P1

"Multiple applications in different namespaces report `bad address` / `no such host`
for every internal name. Nothing was deployed by the app teams today. The pods
themselves are all Running."

This case breaks a **cluster-wide** component. Reset it when you finish.

**Done when:** name resolution works again cluster-wide (`kubectl -n {{NS}} logs deploy/client` says `dns OK`).
