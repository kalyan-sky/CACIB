#!/usr/bin/env bash
source "$(dirname "$0")/../../scripts/lib.sh"
ns=treasury-markets app=risk-calc-engine
leak=$(jp '{.spec.template.spec.containers[0].env[?(@.name=="LEAK_MB_PER_SEC")].value}' deploy $app -n $ns)
[ -z "$leak" ] || [ "$leak" = 0 ] && ok "faulty release no longer deployed" ||
  ko "the running revision still has the memory-hungry cache (LEAK_MB_PER_SEC=$leak)"
deploy_healthy $ns $app && ok "$app: all replicas available" || ko "$app is not available"
restarts=$(jp '{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' pods -n $ns -l app.kubernetes.io/name=$app --field-selector=status.phase=Running | sort -n | tail -1)
[ "${restarts:-0}" = 0 ] && ok "current pods have not restarted" ||
  ko "current pods have restarted $restarts time(s) - is it still OOMing?"
check_result
