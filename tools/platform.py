#!/usr/bin/env python3
"""Platform engineering toolkit for the bank simulation.

    platform.py render    [--profile lite|full] [--out rendered] [--image IMG]
    platform.py audit     [--path rendered | --live] [-n NAMESPACE] [--json]
    platform.py capacity  [--path rendered | --live]
    platform.py inventory [--top 10]
    platform.py restore   <namespace>[/<app>] ...
    platform.py status    (live) nodes, unhealthy workloads, customer-facing SLOs

`render` is the "golden path": every application team gets the same secure,
observable, quota-bound deployment pattern generated from the inventory,
which is how a small platform team keeps 200+ apps consistent.
`audit` checks manifests or the live cluster against the platform's
technology-risk controls. `capacity` compares requests with quotas. `restore` forces live objects
back to their rendered state (used to reset scenarios).
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install -r requirements.txt")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INVENTORY = os.path.join(ROOT, "inventory", "applications.yaml")
DEFAULT_IMAGE = "bank-sim/txn-service:1.5.0"
# Local images (kind), the bank's internal registry, or Amazon ECR (EKS).
APPROVED_REGISTRY = re.compile(r"^(bank-sim/|registry\.bank\.local/|\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/)")
SIM_STATE = os.path.join(ROOT, ".sim-state")
LITE_FILLERS_PER_DOMAIN = 5
TRAFFIC_NS = "sim-traffic"
ZONE_KEY = "topology.kubernetes.io/zone"

DEFAULT_REQ = {"cpu": "10m", "memory": "16Mi"}
DEFAULT_LIM = {"cpu": "100m", "memory": "64Mi"}

# ------------------------------------------------------------------ quantities


def cpu_m(q):
    q = str(q)
    return int(q[:-1]) if q.endswith("m") else int(float(q) * 1000)


_MEM = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "K": 1 / 1000, "M": 1, "G": 1000}


def mem_mi(q):
    m = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", str(q))
    num, unit = float(m.group(1)), m.group(2)
    if unit == "":
        return num / (1024 * 1024)
    return num * _MEM[unit]


def fmt_cpu(m):
    return f"{int(m)}m"


def fmt_mem(mi):
    return f"{int(round(mi))}Mi"


# ------------------------------------------------------------------ inventory


def load_inventory():
    with open(INVENTORY) as f:
        inv = yaml.safe_load(f)
    seen = set()
    for app in inv["applications"]:
        key = (app["domain"], app["name"])
        if key in seen:
            sys.exit(f"duplicate application {key}")
        if app["domain"] not in inv["domains"]:
            sys.exit(f"{app['name']}: unknown domain {app['domain']}")
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]{0,50}[a-z0-9])?", app["name"]):
            sys.exit(f"{app['name']}: not a valid DNS-1035 name")
        seen.add(key)
    for app in inv["applications"]:
        for up in app.get("upstreams", []):
            if tuple(up.split("/", 1)) not in seen:
                sys.exit(f"{app['domain']}/{app['name']}: unknown upstream {up}")
    return inv


def select_profile(inv, profile):
    """lite = core apps + a few fillers per domain + everything they call."""
    if profile == "full":
        return list(inv["applications"])
    by_key = {(a["domain"], a["name"]): a for a in inv["applications"]}
    keys, per_domain = set(), Counter()
    for app in inv["applications"]:
        if app.get("core"):
            keys.add((app["domain"], app["name"]))
        elif per_domain[app["domain"]] < LITE_FILLERS_PER_DOMAIN:
            per_domain[app["domain"]] += 1
            keys.add((app["domain"], app["name"]))
    pending = list(keys)
    while pending:  # keep the dependency graph closed
        for up in by_key[pending.pop()].get("upstreams", []):
            ref = tuple(up.split("/", 1))
            if ref not in keys:
                keys.add(ref)
                pending.append(ref)
    return [a for a in inv["applications"] if (a["domain"], a["name"]) in keys]


def replicas_for(app):
    return app.get("replicas", 2 if app["tier"] == 1 else 1)


def requests_for(app):
    return {**DEFAULT_REQ, **app.get("resources", {})}


def svc_url(ref):
    ns, name = ref.split("/", 1)
    return f"http://{name}.{ns}.svc.cluster.local:8080"


# ------------------------------------------------------------------ cluster access


def kube_context():
    """Same resolution as scripts/lib.sh: $KUBE_CONTEXT, then the context
    recorded by setup.sh / eks-up.sh, then the default kind cluster."""
    return os.environ.get("KUBE_CONTEXT") or recorded("context", "kind-" + os.environ.get("CLUSTER", "bank-sim"))


def recorded(name, default):
    """A value setup.sh / eks-up.sh saved in .sim-state/ (profile, image, context)."""
    try:
        with open(os.path.join(SIM_STATE, name)) as f:
            return f.read().strip() or default
    except OSError:
        return default


def kubectl(*args):
    return ["kubectl", "--context", kube_context(), *args]


# ------------------------------------------------------------------ render


def app_labels(app, dom):
    return {
        "app.kubernetes.io/name": app["name"],
        "app.kubernetes.io/part-of": app["domain"],
        "app.kubernetes.io/managed-by": "bank-platform",
        "bank.sim/tier": f"tier-{app['tier']}",
        "bank.sim/owner-team": dom["team"],
        "bank.sim/cost-centre": dom["cost_centre"],
        "bank.sim/data-classification": app.get("data_classification", dom["data_classification"]),
        **({"bank.sim/exposure": app["exposure"]} if app.get("exposure") else {}),
    }


def render_app(app, dom, image):
    name, ns = app["name"], app["domain"]
    labels = app_labels(app, dom)
    req = requests_for(app)
    lim = {"cpu": fmt_cpu(max(cpu_m(DEFAULT_LIM["cpu"]), cpu_m(req["cpu"]) * 2)),
           "memory": fmt_mem(max(mem_mi(DEFAULT_LIM["memory"]), mem_mi(req["memory"]) * 2))}
    config = {
        "DB_URL": f"postgresql://{name}-db.{ns}.db.bank.local:5432/{name.replace('-', '_')}",
        "UPSTREAMS": ",".join(svc_url(u) for u in app.get("upstreams", [])),
        "LATENCY_MS": "5",
        "FAIL_RATE": "0",
        "LOG_FORMAT": "json",
        **{k: str(v) for k, v in app.get("config", {}).items()},
    }
    pod_spec = {
        "automountServiceAccountToken": False,
        "terminationGracePeriodSeconds": 20,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "app",
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "ports": [{"name": "http", "containerPort": 8080}],
            "env": [
                {"name": "APP_NAME", "value": name},
                {"name": "DOMAIN", "value": ns},
                {"name": "REQUIRED_CONFIG", "value": "DB_URL"},
            ],
            "envFrom": [{"configMapRef": {"name": f"{name}-config"}}],
            "resources": {"requests": req, "limits": lim},
            "readinessProbe": {"httpGet": {"path": "/readyz", "port": "http"},
                               "periodSeconds": 5, "failureThreshold": 3},
            "livenessProbe": {"httpGet": {"path": "/healthz", "port": "http"},
                              "initialDelaySeconds": 5, "periodSeconds": 10, "failureThreshold": 3},
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
        }],
    }
    if app["tier"] == 1:
        pod_spec["topologySpreadConstraints"] = [{
            "maxSkew": 1, "topologyKey": ZONE_KEY, "whenUnsatisfiable": "ScheduleAnyway",
            "labelSelector": {"matchLabels": {"app.kubernetes.io/name": name}},
        }]
    docs = [
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": f"{name}-config", "namespace": ns, "labels": labels},
         "data": config},
        {"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": name, "namespace": ns, "labels": labels,
                      "annotations": {"bank.sim/description": app.get("description", "")}},
         "spec": {
             "replicas": replicas_for(app),
             "revisionHistoryLimit": 5,
             "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
             "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
             "template": {
                 "metadata": {"labels": labels, "annotations": {
                     "prometheus.io/scrape": "true", "prometheus.io/port": "8080",
                     # Changes to the ConfigMap change this hash, which rolls the pods.
                     "bank.sim/config-hash": hashlib.sha256(
                         json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]}},
                 "spec": pod_spec,
             },
         }},
        {"apiVersion": "v1", "kind": "Service",
         "metadata": {"name": name, "namespace": ns, "labels": labels},
         "spec": {"selector": {"app.kubernetes.io/name": name},
                  "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]}},
    ]
    if app["tier"] == 1:
        docs.append({"apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
                     "metadata": {"name": name, "namespace": ns, "labels": labels},
                     "spec": {"maxUnavailable": 1,
                              "selector": {"matchLabels": {"app.kubernetes.io/name": name}}}})
    return docs


def render_namespace(ns, dom, apps, consumers):
    pods = sum(replicas_for(a) for a in apps)
    req_cpu = sum(cpu_m(requests_for(a)["cpu"]) * replicas_for(a) for a in apps)
    req_mem = sum(mem_mi(requests_for(a)["memory"]) * replicas_for(a) for a in apps)
    # Limits are 2x requests with a floor (see render_app); estimate the same way.
    lim_cpu = sum(max(100, cpu_m(requests_for(a)["cpu"]) * 2) * replicas_for(a) for a in apps)
    lim_mem = sum(max(64, mem_mi(requests_for(a)["memory"]) * 2) * replicas_for(a) for a in apps)
    meta_labels = {
        "bank.sim/domain": ns,
        "bank.sim/owner-team": dom["team"],
        "bank.sim/cost-centre": dom["cost_centre"],
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "latest",
        "pod-security.kubernetes.io/warn": "restricted",
        "pod-security.kubernetes.io/audit": "restricted",
    }
    docs = [
        {"apiVersion": "v1", "kind": "Namespace",
         "metadata": {"name": ns, "labels": meta_labels,
                      "annotations": {"bank.sim/description": dom["description"]}}},
        {"apiVersion": "v1", "kind": "ResourceQuota",
         "metadata": {"name": "compute-quota", "namespace": ns},
         "spec": {"hard": {
             # 25% headroom for rolling updates plus a fixed allowance for new onboarding.
             "requests.cpu": fmt_cpu(req_cpu * 1.25 + 500),
             "requests.memory": fmt_mem(req_mem * 1.25 + 512),
             "limits.cpu": fmt_cpu(lim_cpu * 1.25 + 1000),
             "limits.memory": fmt_mem(lim_mem * 1.25 + 1024),
             "pods": str(pods * 2 + 10),
         }}},
        {"apiVersion": "v1", "kind": "LimitRange",
         "metadata": {"name": "container-defaults", "namespace": ns},
         "spec": {"limits": [{"type": "Container", "default": DEFAULT_LIM, "defaultRequest": DEFAULT_REQ,
                              "max": {"cpu": "2", "memory": "2Gi"}}]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": {"name": "default-deny-ingress", "namespace": ns},
         "spec": {"podSelector": {}, "policyTypes": ["Ingress"]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": {"name": "allow-same-namespace", "namespace": ns},
         "spec": {"podSelector": {}, "policyTypes": ["Ingress"],
                  "ingress": [{"from": [{"podSelector": {}}]}]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": {"name": "allow-edge-from-traffic-tier", "namespace": ns},
         "spec": {"podSelector": {"matchLabels": {"bank.sim/exposure": "edge"}}, "policyTypes": ["Ingress"],
                  "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {
                      "kubernetes.io/metadata.name": TRAFFIC_NS}}}]}]}},
    ]
    if consumers:
        docs.append({
            "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "allow-from-consumers", "namespace": ns,
                         "annotations": {"bank.sim/generated-from": "inventory upstreams"}},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"],
                     "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {
                         "kubernetes.io/metadata.name": c}}} for c in sorted(consumers)],
                         "ports": [{"protocol": "TCP", "port": 8080}]}]}})
    docs.append({
        "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
        "metadata": {"name": "app-team-deployers", "namespace": ns},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "bank-sim:app-deployer"},
        "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": dom["team"]}],
    })
    return docs


def render_traffic(edge_apps, image):
    labels = {"app.kubernetes.io/name": "traffic-simulator", "app.kubernetes.io/managed-by": "bank-platform"}
    targets = ",".join(svc_url(f"{a['domain']}/{a['name']}") for a in edge_apps)
    return [
        {"apiVersion": "v1", "kind": "Namespace",
         "metadata": {"name": TRAFFIC_NS, "labels": {
             "pod-security.kubernetes.io/enforce": "restricted",
             "pod-security.kubernetes.io/enforce-version": "latest"}}},
        {"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": "traffic-simulator", "namespace": TRAFFIC_NS, "labels": labels},
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": "traffic-simulator"}},
                  "template": {"metadata": {"labels": labels}, "spec": {
                      "automountServiceAccountToken": False,
                      "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                          "seccompProfile": {"type": "RuntimeDefault"}},
                      "containers": [{
                          "name": "loadgen", "image": image, "imagePullPolicy": "IfNotPresent",
                          "args": ["loadgen"],
                          "env": [{"name": "TARGETS", "value": targets}, {"name": "RPS", "value": "2"}],
                          "ports": [{"name": "http", "containerPort": 8080}],
                          "resources": {"requests": {"cpu": "20m", "memory": "32Mi"},
                                        "limits": {"cpu": "200m", "memory": "128Mi"}},
                          "readinessProbe": {"httpGet": {"path": "/readyz", "port": "http"}},
                          "livenessProbe": {"httpGet": {"path": "/healthz", "port": "http"}},
                          "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                              "capabilities": {"drop": ["ALL"]}},
                      }]}}}},
        {"apiVersion": "v1", "kind": "Service",
         "metadata": {"name": "traffic-simulator", "namespace": TRAFFIC_NS, "labels": labels},
         "spec": {"selector": {"app.kubernetes.io/name": "traffic-simulator"},
                  "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]}},
    ]


# The corporate e-banking portal (web tier) and the edge services it calls.
PORTAL_BACKENDS = {
    "ACCOUNTS_URL": "channels/corporate-portal-api",
    "PAYMENTS_URL": "payments/payments-gateway",
    "PAYNOW_URL": "payments/paynow-fast-adapter",
    "TRADE_URL": "trade-finance/lc-issuance",
    "KYC_URL": "risk-compliance/kyc-onboarding",
}


def render_portal(image):
    """bank-portal: UI + backend-for-frontend, in the traffic (web) tier.

    It keeps demo client state in memory, so it runs as a single replica; a
    real portal would keep sessions and state in a database or cache.
    """
    labels = {"app.kubernetes.io/name": "bank-portal", "app.kubernetes.io/managed-by": "bank-platform",
              "app.kubernetes.io/part-of": "channels"}
    return [
        {"apiVersion": "apps/v1", "kind": "Deployment",
         "metadata": {"name": "bank-portal", "namespace": TRAFFIC_NS, "labels": labels},
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": "bank-portal"}},
                  "strategy": {"type": "Recreate"},  # single in-memory instance
                  "template": {"metadata": {"labels": labels}, "spec": {
                      "automountServiceAccountToken": False,
                      "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                          "seccompProfile": {"type": "RuntimeDefault"}},
                      "containers": [{
                          "name": "portal", "image": image, "imagePullPolicy": "IfNotPresent",
                          "args": ["portal"],
                          "env": [{"name": k, "value": svc_url(v)} for k, v in PORTAL_BACKENDS.items()],
                          "ports": [{"name": "http", "containerPort": 8080}],
                          "resources": {"requests": {"cpu": "20m", "memory": "32Mi"},
                                        "limits": {"cpu": "300m", "memory": "128Mi"}},
                          "readinessProbe": {"httpGet": {"path": "/readyz", "port": "http"}},
                          "livenessProbe": {"httpGet": {"path": "/healthz", "port": "http"}},
                          "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                              "capabilities": {"drop": ["ALL"]}},
                      }]}}}},
        {"apiVersion": "v1", "kind": "Service",
         "metadata": {"name": "bank-portal", "namespace": TRAFFIC_NS, "labels": labels},
         "spec": {"selector": {"app.kubernetes.io/name": "bank-portal"},
                  "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]}},
    ]


def display_path(path):
    path = os.path.abspath(path)
    return os.path.relpath(path, ROOT) if path.startswith(ROOT + os.sep) else path


def write_yaml(path, docs):
    with open(path, "w") as f:
        f.write("# GENERATED by tools/platform.py render - edit inventory/applications.yaml instead\n")
        yaml.safe_dump_all(docs, f, sort_keys=False, explicit_start=True)


def cmd_render(args):
    inv = load_inventory()
    apps = select_profile(inv, args.profile)
    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    by_ns = defaultdict(list)
    consumers = defaultdict(set)
    for a in apps:
        by_ns[a["domain"]].append(a)
        for up in a.get("upstreams", []):
            up_ns = up.split("/", 1)[0]
            if up_ns != a["domain"]:
                consumers[up_ns].add(a["domain"])
    for ns, ns_apps in by_ns.items():
        d = os.path.join(args.out, ns)
        os.makedirs(d)
        write_yaml(os.path.join(d, "00-namespace.yaml"),
                   render_namespace(ns, inv["domains"][ns], ns_apps, consumers[ns]))
        for a in ns_apps:
            write_yaml(os.path.join(d, f"{a['name']}.yaml"), render_app(a, inv["domains"][ns], args.image))
    os.makedirs(os.path.join(args.out, TRAFFIC_NS))
    write_yaml(os.path.join(args.out, TRAFFIC_NS, "traffic-simulator.yaml"),
               render_traffic([a for a in apps if a.get("exposure") == "edge"], args.image))
    write_yaml(os.path.join(args.out, TRAFFIC_NS, "bank-portal.yaml"), render_portal(args.image))
    pods = sum(replicas_for(a) for a in apps)
    print(f"rendered profile={args.profile}: {len(apps)} applications, {pods} pods, "
          f"{len(by_ns)} namespaces -> {display_path(args.out)}/")


# ------------------------------------------------------------------ audit

CONTROLS = {
    "C01": "Containers declare CPU/memory requests and limits",
    "C02": "Containers have liveness and readiness probes",
    "C03": "Hardened securityContext (non-root, no privilege escalation, drop ALL, read-only rootfs)",
    "C04": "Image from an approved registry and pinned (no :latest)",
    "C05": "Ownership labels present (owner-team, tier, cost-centre, data-classification)",
    "C06": "Tier-1 resilience: >=2 replicas, PodDisruptionBudget, zone spread",
    "C07": "Namespace guardrails: PSA restricted, default-deny ingress, ResourceQuota",
}
REQUIRED_LABELS = ["bank.sim/owner-team", "bank.sim/tier", "bank.sim/cost-centre", "bank.sim/data-classification"]
SKIP_NS = {"kube-system", "kube-public", "kube-node-lease", "local-path-storage", TRAFFIC_NS}


def load_objects(args):
    if args.live:
        objs = []
        for kind in ["namespaces", "deployments", "poddisruptionbudgets", "networkpolicies", "resourcequotas"]:
            cmd = kubectl("get", kind, "-o", "json") + ([] if kind == "namespaces" else ["-A"])
            out = subprocess.run(cmd, capture_output=True, text=True)
            if out.returncode:
                sys.exit(f"kubectl get {kind} failed: {out.stderr.strip()}")
            objs.extend(json.loads(out.stdout)["items"])
        for o in objs:  # list items omit kind for some clients
            o.setdefault("kind", "")
        return objs
    objs = []
    for dirpath, _, files in os.walk(args.path):
        for fn in sorted(files):
            if fn.endswith((".yaml", ".yml")):
                with open(os.path.join(dirpath, fn)) as f:
                    objs.extend(d for d in yaml.safe_load_all(f) if d)
    return objs


def audit_deployment(dep, pdbs):
    findings = []
    meta, spec = dep["metadata"], dep["spec"]
    pod = spec["template"]["spec"]
    labels = spec["template"]["metadata"].get("labels", {})
    psc = pod.get("securityContext", {})

    def add(ctrl, msg):
        findings.append((ctrl, msg))

    for c in pod.get("containers", []) + pod.get("initContainers", []):
        cn = c["name"]
        res = c.get("resources", {})
        for k in ("requests", "limits"):
            for r in ("cpu", "memory"):
                if r not in res.get(k, {}):
                    add("C01", f"container {cn}: missing {k}.{r}")
        if c in pod.get("containers", []):
            for p in ("livenessProbe", "readinessProbe"):
                if p not in c:
                    add("C02", f"container {cn}: missing {p}")
        sc = c.get("securityContext", {})
        if sc.get("privileged"):
            add("C03", f"container {cn}: privileged: true")
        if not (sc.get("runAsNonRoot", psc.get("runAsNonRoot"))):
            add("C03", f"container {cn}: runAsNonRoot not set")
        if sc.get("runAsUser", psc.get("runAsUser")) == 0:
            add("C03", f"container {cn}: runs as UID 0")
        if sc.get("allowPrivilegeEscalation", True) is not False:
            add("C03", f"container {cn}: allowPrivilegeEscalation not false")
        if "ALL" not in sc.get("capabilities", {}).get("drop", []):
            add("C03", f"container {cn}: capabilities not dropped")
        if sc.get("capabilities", {}).get("add"):
            add("C03", f"container {cn}: adds capabilities {sc['capabilities']['add']}")
        if not sc.get("readOnlyRootFilesystem"):
            add("C03", f"container {cn}: root filesystem writable")
        img = c.get("image", "")
        if not APPROVED_REGISTRY.match(img):
            add("C04", f"container {cn}: image {img} not from approved registry")
        if ":" not in img.rsplit("/", 1)[-1] or img.endswith(":latest"):
            add("C04", f"container {cn}: image {img} not pinned to a version")
    for p in ("hostNetwork", "hostPID", "hostIPC"):
        if pod.get(p):
            add("C03", f"pod uses {p}")
    for lab in REQUIRED_LABELS:
        if lab not in labels:
            add("C05", f"missing label {lab}")
    if labels.get("bank.sim/tier") == "tier-1":
        if spec.get("replicas", 1) < 2:
            add("C06", f"tier-1 with {spec.get('replicas', 1)} replica(s)")
        sel = spec["selector"].get("matchLabels", {})
        if not any(p["metadata"]["namespace"] == meta["namespace"] and
                   p["spec"].get("selector", {}).get("matchLabels") == sel for p in pdbs):
            add("C06", "tier-1 without PodDisruptionBudget")
        if not any(t.get("topologyKey") == ZONE_KEY for t in pod.get("topologySpreadConstraints", [])):
            add("C06", "tier-1 without zone topologySpreadConstraints")
    return findings


def cmd_audit(args):
    objs = load_objects(args)
    by_kind = defaultdict(list)
    for o in objs:
        kind = o.get("kind") or ("Deployment" if "template" in o.get("spec", {}) else "")
        by_kind[kind].append(o)
    pdbs = by_kind["PodDisruptionBudget"]
    results = []  # (namespace, object, control, message)

    for ns in by_kind["Namespace"]:
        name = ns["metadata"]["name"]
        if name in SKIP_NS or name == "default" or (args.namespace and name != args.namespace):
            continue
        if ns["metadata"].get("labels", {}).get("pod-security.kubernetes.io/enforce") != "restricted":
            results.append((name, f"namespace/{name}", "C07", "Pod Security Admission not enforcing 'restricted'"))
        if not any(n["metadata"]["namespace"] == name and n["spec"].get("podSelector") == {} and
                   "Ingress" in n["spec"].get("policyTypes", []) and not n["spec"].get("ingress")
                   for n in by_kind["NetworkPolicy"]):
            results.append((name, f"namespace/{name}", "C07", "no default-deny ingress NetworkPolicy"))
        if not any(q["metadata"]["namespace"] == name for q in by_kind["ResourceQuota"]):
            results.append((name, f"namespace/{name}", "C07", "no ResourceQuota"))

    deployments = [d for d in by_kind["Deployment"] if d["metadata"]["namespace"] not in SKIP_NS
                   and (not args.namespace or d["metadata"]["namespace"] == args.namespace)]
    for dep in deployments:
        for ctrl, msg in audit_deployment(dep, pdbs):
            results.append((dep["metadata"]["namespace"], f"deployment/{dep['metadata']['name']}", ctrl, msg))

    if args.json:
        print(json.dumps([{"namespace": n, "object": o, "control": c, "finding": m} for n, o, c, m in results], indent=2))
        return 1 if results else 0

    print(f"Platform control audit ({'live cluster' if args.live else args.path}) - "
          f"{len(deployments)} deployments, {len(by_kind['Namespace'])} namespaces\n")
    per_ctrl = Counter(c for _, _, c, _ in results)
    for ctrl, desc in CONTROLS.items():
        status = "PASS" if per_ctrl[ctrl] == 0 else f"FAIL ({per_ctrl[ctrl]})"
        print(f"  {ctrl}  {status:<10} {desc}")
    if results:
        print("\nFindings:")
        for n, o, c, m in sorted(results):
            print(f"  [{c}] {n}/{o}: {m}")
        print(f"\n{len(results)} finding(s). Non-compliant workloads must be remediated or risk-accepted.")
        return 1
    print("\nAll controls passed.")
    return 0


# ------------------------------------------------------------------ capacity


def cmd_capacity(args):
    objs = load_objects(args) if not args.live else None
    usage = defaultdict(lambda: {"pods": 0, "cpu": 0, "mem": 0, "apps": 0})
    quotas = {}
    if args.live:
        pods = json.loads(subprocess.run(kubectl("get", "pods", "-A", "-o", "json"),
                                         capture_output=True, text=True, check=True).stdout)["items"]
        for p in pods:
            ns = p["metadata"]["namespace"]
            if ns in SKIP_NS or p["status"].get("phase") not in ("Running", "Pending"):
                continue
            u = usage[ns]
            u["pods"] += 1
            for c in p["spec"]["containers"]:
                r = c.get("resources", {}).get("requests", {})
                u["cpu"] += cpu_m(r.get("cpu", "0m"))
                u["mem"] += mem_mi(r.get("memory", "0Mi"))
        rq = json.loads(subprocess.run(kubectl("get", "resourcequota", "-A", "-o", "json"),
                                       capture_output=True, text=True, check=True).stdout)["items"]
        objs = rq
    for o in objs:
        kind, ns = o.get("kind"), o["metadata"].get("namespace")
        if "hard" in o.get("spec", {}):
            quotas[ns] = o["spec"]["hard"]
        if kind == "Deployment" and not args.live and ns not in SKIP_NS:
            rep = o["spec"].get("replicas", 1)
            u = usage[ns]
            u["apps"] += 1
            u["pods"] += rep
            for c in o["spec"]["template"]["spec"]["containers"]:
                r = c.get("resources", {}).get("requests", {})
                u["cpu"] += cpu_m(r.get("cpu", "0m")) * rep
                u["mem"] += mem_mi(r.get("memory", "0Mi")) * rep

    print(f"{'namespace':<18}{'pods':>6}{'cpu req':>10}{'cpu quota':>11}{'cpu %':>7}{'mem req':>10}{'mem quota':>11}{'mem %':>7}")
    tot = Counter()
    for ns in sorted(usage):
        u, q = usage[ns], quotas.get(ns, {})
        qc, qm = cpu_m(q.get("requests.cpu", "0")), mem_mi(q.get("requests.memory", "0"))
        pc = f"{100 * u['cpu'] / qc:.0f}%" if qc else "-"
        pm = f"{100 * u['mem'] / qm:.0f}%" if qm else "-"
        flag = "  <-- near quota" if (qc and u["cpu"] / qc > 0.85) or (qm and u["mem"] / qm > 0.85) else ""
        print(f"{ns:<18}{u['pods']:>6}{fmt_cpu(u['cpu']):>10}{fmt_cpu(qc) if qc else '-':>11}{pc:>7}"
              f"{fmt_mem(u['mem']):>10}{fmt_mem(qm) if qm else '-':>11}{pm:>7}{flag}")
        tot["pods"] += u["pods"]
        tot["cpu"] += u["cpu"]
        tot["mem"] += u["mem"]
    print(f"{'TOTAL':<18}{tot['pods']:>6}{fmt_cpu(tot['cpu']):>10}{'':>11}{'':>7}{fmt_mem(tot['mem']):>10}")


# ------------------------------------------------------------------ inventory


def cmd_inventory(args):
    inv = load_inventory()
    apps = inv["applications"]
    print(f"{len(apps)} applications in {len(inv['domains'])} business domains\n")
    print(f"{'domain':<18}{'apps':>5}{'tier-1':>8}{'tier-2':>8}{'tier-3':>8}  owner")
    for d, meta in inv["domains"].items():
        t = Counter(a["tier"] for a in apps if a["domain"] == d)
        print(f"{d:<18}{sum(t.values()):>5}{t[1]:>8}{t[2]:>8}{t[3]:>8}  {meta['team']}")
    # Blast radius: how many apps transitively depend on each service.
    callers = defaultdict(set)
    for a in apps:
        for up in a.get("upstreams", []):
            callers[up].add(f"{a['domain']}/{a['name']}")

    def radius(ref, seen=None):
        seen = set() if seen is None else seen
        for c in callers.get(ref, ()):
            if c not in seen:
                seen.add(c)
                radius(c, seen)
        return seen

    ranked = sorted(((len(radius(f"{a['domain']}/{a['name']}")), f"{a['domain']}/{a['name']}") for a in apps), reverse=True)
    print(f"\nTop {args.top} services by blast radius (apps that break if it is down):")
    for n, ref in ranked[:args.top]:
        print(f"  {n:>4}  {ref}")
    print("\nCustomer-facing entry points:", ", ".join(a["name"] for a in apps if a.get("exposure") == "edge"))


# ------------------------------------------------------------------ restore


def restore_object(obj):
    """Make one live object identical to its rendered form.

    `replace` discards any drift (extra env vars, patched fields) that a
    client-side `apply` would merge with and keep; missing objects are
    created. Services are applied instead, because a replace would have to
    carry the allocated clusterIP.
    """
    kube = kubectl()
    body = json.dumps(obj)
    what = f"{obj['kind']}/{obj['metadata']['name']}"
    verb = ["apply", "-f", "-"] if obj["kind"] == "Service" else ["replace", "--save-config", "-f", "-"]
    for _ in range(5):
        out = subprocess.run(kube + verb, input=body, capture_output=True, text=True)
        if out.returncode == 0:
            return
        if "NotFound" in out.stderr or "not found" in out.stderr:
            verb = ["create", "--save-config", "-f", "-"]
        elif "Conflict" not in out.stderr and "modified" not in out.stderr:
            break  # controllers updating status cause conflicts; anything else is real
        time.sleep(1)
    sys.exit(f"kubectl {verb[0]} {what} failed: {out.stderr.strip()}")


def cmd_restore(args):
    for ref in args.refs:
        ns, _, app = ref.partition("/")
        path = os.path.join(args.path, ns, f"{app}.yaml" if app else "00-namespace.yaml")
        if not os.path.exists(path):
            sys.exit(f"{path} not found - is {ref} in the inventory and rendered?")
        with open(path) as f:
            docs = [d for d in yaml.safe_load_all(f) if d]
        for d in docs:
            restore_object(d)
        print(f"restored {ref} ({len(docs)} objects)")


# ------------------------------------------------------------------ status


def kget(*args):
    try:
        out = subprocess.run(kubectl(*args), capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("kubectl not found on PATH")
    if out.returncode:
        raise RuntimeError(out.stderr.strip())
    return out.stdout


def pod_problem(pod):
    """Best one-line explanation of why a pod is not healthy, or None."""
    st = pod.get("status", {})
    for c in st.get("containerStatuses", []) + st.get("initContainerStatuses", []):
        w = c.get("state", {}).get("waiting")
        if w and w.get("reason") not in (None, "ContainerCreating", "PodInitializing"):
            last = c.get("lastState", {}).get("terminated", {}).get("reason")
            return w["reason"] + (f" (last exit: {last})" if last else "")
        if not c.get("ready") and c.get("state", {}).get("running"):
            return "Running but not Ready (readiness probe failing)"
    for cond in st.get("conditions", []):
        if cond["type"] == "PodScheduled" and cond["status"] == "False":
            return f"Unschedulable: {cond.get('message', '')[:90]}"
    if st.get("phase") == "Pending":
        return "Pending"
    return None


def cmd_status(args):
    try:
        nodes = json.loads(kget("get", "nodes", "-o", "json"))["items"]
        deps = json.loads(kget("get", "deployments", "-A", "-o", "json"))["items"]
        pods = json.loads(kget("get", "pods", "-A", "-o", "json"))["items"]
    except RuntimeError as e:
        sys.exit(f"cannot reach cluster: {e}")

    print("NODES")
    width = max([len(n["metadata"]["name"]) for n in nodes] + [20]) + 2
    for n in nodes:
        ready = next((c["status"] for c in n["status"]["conditions"] if c["type"] == "Ready"), "?")
        flags = []
        if ready != "True":
            flags.append("NotReady")
        if n["spec"].get("unschedulable"):
            flags.append("Cordoned")
        zone = n["metadata"]["labels"].get(ZONE_KEY, "-")
        print(f"  {n['metadata']['name']:<{width}}{zone:<18}{', '.join(flags) or 'Ready'}")

    bank = [d for d in deps if d["metadata"]["namespace"] not in SKIP_NS | {"default"}]
    unhealthy = []
    for d in bank:
        want = d["spec"].get("replicas", 1)
        stt = d.get("status", {})
        if stt.get("availableReplicas", 0) >= want and stt.get("updatedReplicas", 0) == want \
                and not stt.get("unavailableReplicas"):
            continue
        sel = d["spec"]["selector"].get("matchLabels", {})
        problems = Counter()
        for p in pods:
            if p["metadata"]["namespace"] == d["metadata"]["namespace"] and \
                    all(p["metadata"].get("labels", {}).get(k) == v for k, v in sel.items()):
                prob = pod_problem(p)
                if prob:
                    problems[prob] += 1
        rs_issue = next((c.get("message", "") for c in stt.get("conditions", [])
                         if c["type"] == "ReplicaFailure" and c["status"] == "True"), "")
        why = "; ".join(f"{n}x {r}" for r, n in problems.items()) or rs_issue[:110] or "rollout in progress"
        unhealthy.append((d["metadata"]["namespace"], d["metadata"]["name"],
                          f"{stt.get('availableReplicas', 0)}/{want}", why))
    print(f"\nWORKLOADS  {len(bank) - len(unhealthy)}/{len(bank)} deployments healthy")
    for ns, name, avail, why in sorted(unhealthy):
        print(f"  {ns + '/' + name:<44}{avail:>6}  {why}")

    print("\nCUSTOMER-FACING FLOWS (synthetic traffic, last 60s)")
    try:
        stats = json.loads(kget("get", "--raw",
                                f"/api/v1/namespaces/{TRAFFIC_NS}/services/traffic-simulator:http/proxy/stats"))
    except (RuntimeError, ValueError) as e:
        print(f"  traffic simulator unavailable: {str(e)[:100]}")
        return 0
    for s in stats:
        name = s["target"].split("//")[1].split(".svc")[0]
        err = f"  last error: {s['last_error'][:70]}" if s["status"] != "HEALTHY" and s.get("last_error") else ""
        print(f"  {name:<44}{s['status']:<10}{s['success_pct_60s']:>6}%  p95 {s['p95_ms_60s']}ms{err}")
    return 0


# ------------------------------------------------------------------ main


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render", help="generate manifests from the inventory")
    r.add_argument("--profile", choices=["lite", "full"], default=os.environ.get("PROFILE") or recorded("profile", "lite"))
    r.add_argument("--out", default=os.path.join(ROOT, "rendered"))
    r.add_argument("--image", default=recorded("image", DEFAULT_IMAGE),
                   help="default: the image recorded by setup.sh / eks-up.sh")
    for name in ("audit", "capacity"):
        a = sub.add_parser(name)
        src = a.add_mutually_exclusive_group()
        src.add_argument("--path", default=os.path.join(ROOT, "rendered"))
        src.add_argument("--live", action="store_true", help="read from the current kubectl context")
        if name == "audit":
            a.add_argument("-n", "--namespace")
            a.add_argument("--json", action="store_true")
    i = sub.add_parser("inventory", help="summarise the application estate")
    i.add_argument("--top", type=int, default=10)
    rs = sub.add_parser("restore", help="reset live objects to their rendered state")
    rs.add_argument("refs", nargs="+", metavar="namespace[/app]")
    rs.add_argument("--path", default=os.path.join(ROOT, "rendered"))
    sub.add_parser("status", help="live estate health and SLOs")
    args = p.parse_args()
    rc = {"status": cmd_status, "render": cmd_render, "audit": cmd_audit, "capacity": cmd_capacity, "inventory": cmd_inventory,
          "restore": cmd_restore}[args.cmd](args)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
