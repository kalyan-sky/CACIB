"""AWS Lambda front door for the BankSim corporate banking portal.

    browser --HTTPS--> Lambda function URL (this file)
                          |  serves the UI files bundled at deploy time
                          |  forwards /api/* inside the VPC
                          v
                 internal load balancer (no public address)
                          v
                 bank-portal backend-for-frontend in EKS

Nothing in the cluster is reachable from the internet; only this function is,
and only for the source addresses listed in ALLOWED_CIDRS.

Environment:
    BACKEND_URL     http://<internal-load-balancer-dns>  (required)
    ALLOWED_CIDRS   comma-separated CIDRs allowed to use the portal (required)
"""
import base64
import ipaddress
import json
import mimetypes
import os
import pathlib
import socket
import urllib.error
import urllib.request

WEB_ROOT = pathlib.Path(__file__).resolve().parent / "web"
BACKEND_URL = os.environ.get("BACKEND_URL", "").rstrip("/")
ALLOWED = [ipaddress.ip_network(c.strip(), strict=False)
           for c in os.environ.get("ALLOWED_CIDRS", "").split(",") if c.strip()]
MAX_BODY = 64 * 1024
FORWARDED_HEADERS = ("content-type", "accept", "x-demo-user")
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def _load_static():
    files = {}
    if WEB_ROOT.is_dir():
        for f in WEB_ROOT.rglob("*"):
            if f.is_file():
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                    ctype += "; charset=utf-8"
                files["/" + f.relative_to(WEB_ROOT).as_posix()] = (f.read_bytes(), ctype)
    files["/"] = files.get("/index.html", (b"portal UI not bundled", "text/plain"))
    return files


STATIC = _load_static()  # loaded once per cold start


def _response(status, body, ctype="application/json", extra=None):
    headers = dict(SECURITY_HEADERS)
    headers["Content-Type"] = ctype
    headers.update(extra or {})
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
    if isinstance(body, bytes):
        if ctype.startswith(("text/", "application/javascript", "application/json")):
            body = body.decode("utf-8")
        else:
            return {"statusCode": status, "headers": headers, "isBase64Encoded": True,
                    "body": base64.b64encode(body).decode()}
    return {"statusCode": status, "headers": headers, "body": body}


def _allowed(source_ip):
    try:
        ip = ipaddress.ip_address(source_ip)
    except ValueError:
        return False
    return any(ip in net for net in ALLOWED)


def _proxy(event, method, path):
    if not BACKEND_URL:
        return _response(503, {"error": "Portal backend not configured"})
    body = event.get("body")
    if body and event.get("isBase64Encoded"):
        body = base64.b64decode(body)
    elif body:
        body = body.encode("utf-8")
    if body and len(body) > MAX_BODY:
        return _response(413, {"error": "Request too large"})
    query = event.get("rawQueryString")
    url = BACKEND_URL + path + ("?" + query if query else "")
    headers = {k: v for k, v in (event.get("headers") or {}).items() if k.lower() in FORWARDED_HEADERS}
    req = urllib.request.Request(url, data=body if method != "GET" else None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            status, data, ctype = resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as e:  # 4xx/5xx from the portal: pass through unchanged
        status, data, ctype = e.code, e.read(), e.headers.get("Content-Type", "application/json")
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        print(json.dumps({"level": "ERROR", "msg": "backend unreachable", "url": url, "error": str(e)}))
        return _response(502, {"error": "The banking service is temporarily unavailable. Please try again later."})
    return _response(status, data, ctype, {"Cache-Control": "no-store"})


def handler(event, context):
    http = (event.get("requestContext") or {}).get("http") or {}
    method, source_ip = http.get("method", "GET"), http.get("sourceIp", "")
    path = event.get("rawPath") or "/"

    if not _allowed(source_ip):
        print(json.dumps({"level": "WARN", "msg": "blocked by IP allow-list", "source_ip": source_ip, "path": path}))
        return _response(403, "Forbidden: your IP address is not on the allow-list for this demo portal.\n",
                         "text/plain; charset=utf-8")
    if path.startswith("/api/"):
        if method not in ("GET", "POST"):
            return _response(405, {"error": "method not allowed"})
        return _proxy(event, method, path)
    if method != "GET":
        return _response(405, {"error": "method not allowed"})
    if path in STATIC:
        data, ctype = STATIC[path]
        return _response(200, data, ctype, {"Cache-Control": "no-cache"})
    return _response(404, "Not found\n", "text/plain; charset=utf-8")
