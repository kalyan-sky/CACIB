"""Tests for the Lambda front door, against a fake portal backend.

    python3 -m unittest discover -s lambda/portal-edge
"""
import http.server
import importlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest

HERE = pathlib.Path(__file__).resolve().parent


class FakePortal(http.server.BaseHTTPRequestHandler):
    def _reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/accounts"):
            return self._reply(503, {"error": "Accounts unavailable"})
        self._reply(200, {"path": self.path, "user": self.headers.get("X-Demo-User"),
                          "cookie": self.headers.get("Cookie")})

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self._reply(201, {"echo": json.loads(body), "ctype": self.headers.get("Content-Type")})

    def log_message(self, *args):
        pass


def event(path, ip="203.0.113.7", method="GET", body=None, headers=None, query=""):
    return {"rawPath": path, "rawQueryString": query, "headers": headers or {}, "body": body,
            "isBase64Encoded": False, "requestContext": {"http": {"method": method, "sourceIp": ip}}}


class HandlerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), FakePortal)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        # bundle the real UI next to a copy of the handler, as the deploy script does
        cls.tmp = tempfile.mkdtemp()
        shutil.copy(HERE / "handler.py", cls.tmp)
        shutil.copytree(HERE.parent.parent / "apps" / "txn-service" / "web", pathlib.Path(cls.tmp) / "web")
        os.environ["BACKEND_URL"] = f"http://127.0.0.1:{cls.server.server_port}"
        os.environ["ALLOWED_CIDRS"] = "203.0.113.0/24, 2001:db8::/32"
        sys.path.insert(0, cls.tmp)
        cls.h = importlib.import_module("handler")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.tmp)

    def test_blocks_ips_outside_allow_list(self):
        r = self.h.handler(event("/", ip="198.51.100.9"), None)
        self.assertEqual(r["statusCode"], 403)
        self.assertEqual(self.h.handler(event("/", ip="not-an-ip"), None)["statusCode"], 403)
        self.assertEqual(self.h.handler(event("/", ip="2001:db8::1"), None)["statusCode"], 200)

    def test_serves_ui_with_security_headers(self):
        r = self.h.handler(event("/"), None)
        self.assertEqual(r["statusCode"], 200)
        self.assertIn("BankSim", r["body"])
        self.assertIn("frame-ancestors 'none'", r["headers"]["Content-Security-Policy"])
        self.assertIn("max-age", r["headers"]["Strict-Transport-Security"])
        js = self.h.handler(event("/app.js"), None)
        self.assertTrue(js["headers"]["Content-Type"].startswith(("application/javascript", "text/javascript")))
        self.assertEqual(self.h.handler(event("/../handler.py"), None)["statusCode"], 404)

    def test_forwards_api_get_with_user_and_query_only_allowed_headers(self):
        r = self.h.handler(event("/api/transactions", query="account=sgd-ops",
                                 headers={"x-demo-user": "alex", "cookie": "secret=1"}), None)
        body = json.loads(r["body"])
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(body["path"], "/api/transactions?account=sgd-ops")
        self.assertEqual(body["user"], "alex")
        self.assertIsNone(body["cookie"], "headers outside the allow-list must not be forwarded")
        self.assertEqual(r["headers"]["Cache-Control"], "no-store")

    def test_forwards_api_post_body(self):
        r = self.h.handler(event("/api/payments", method="POST", body=json.dumps({"amount": "10"}),
                                 headers={"content-type": "application/json"}), None)
        self.assertEqual(r["statusCode"], 201)
        self.assertEqual(json.loads(r["body"])["echo"], {"amount": "10"})

    def test_passes_backend_errors_through(self):
        r = self.h.handler(event("/api/accounts"), None)
        self.assertEqual(r["statusCode"], 503)
        self.assertEqual(json.loads(r["body"])["error"], "Accounts unavailable")

    def test_backend_down_gives_customer_safe_502(self):
        saved = self.h.BACKEND_URL
        self.h.BACKEND_URL = "http://127.0.0.1:1"
        try:
            r = self.h.handler(event("/api/meta"), None)
        finally:
            self.h.BACKEND_URL = saved
        self.assertEqual(r["statusCode"], 502)
        self.assertNotIn("127.0.0.1", r["body"])

    def test_rejects_other_methods_and_large_bodies(self):
        self.assertEqual(self.h.handler(event("/api/payments", method="DELETE"), None)["statusCode"], 405)
        big = event("/api/payments", method="POST", body="x" * (64 * 1024 + 1))
        self.assertEqual(self.h.handler(big, None)["statusCode"], 413)


if __name__ == "__main__":
    unittest.main()
