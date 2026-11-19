# python3 test_portal.py   - portal.py against a stand-in API (standard library only)
import json, socket, subprocess, sys, threading, time, unittest, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeApi(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def answer(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.dumps({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                           "ctype": self.headers.get("Content-Type"), "body": self.rfile.read(n).decode() if n else ""}).encode()
        code = 404 if "missing" in self.path else 204 if self.command == "DELETE" else 200
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "0" if code == 204 else str(len(body))); self.end_headers()
        if code != 204: self.wfile.write(body)
    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = answer


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class PortalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=cls.api.serve_forever, daemon=True).start()
        cls.port = free_port()
        cls.addon = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)        # an add-on service answers the same way
        threading.Thread(target=cls.addon.serve_forever, daemon=True).start()
        cls.proc = subprocess.Popen([sys.executable, "portal.py", "--api", f"http://127.0.0.1:{cls.api.server_port}",
                                     "--public-api", "http://lab-pc:36100", "--bind", "127.0.0.1", "--port", str(cls.port), "--quiet",
                                     "--addon", f"containers=http://127.0.0.1:{cls.addon.server_port}"])
        for _ in range(50):
            try: socket.create_connection(("127.0.0.1", cls.port), 0.2).close(); break
            except OSError: time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate(); cls.proc.wait(); cls.api.shutdown(); cls.addon.shutdown()

    def call(self, path, method="GET", data=None, headers={}):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req) as r: return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e: return e.code, e.read(), e.headers

    def test_page_and_config(self):
        code, body, h = self.call("/")
        self.assertEqual(code, 200); self.assertIn(b"vdc-cluster console", body)
        self.assertEqual(json.loads(self.call("/config.json")[1]), {"api": "http://lab-pc:36100"})
        self.assertEqual(self.call("/nothing")[0], 404)

    def test_api_calls_pass_through(self):
        code, body, _ = self.call("/tenant/vms?state=true", headers={"Authorization": "Bearer t0k"})
        r = json.loads(body)
        self.assertEqual((code, r["path"], r["auth"]), (200, "/tenant/vms?state=true", "Bearer t0k"))
        code, body, _ = self.call("/tenant/vms", "POST", b'{"name":"a1"}', {"Content-Type": "application/json"})
        r = json.loads(body)
        self.assertEqual((r["method"], r["body"], r["ctype"]), ("POST", '{"name":"a1"}', "application/json"))
        self.assertEqual(self.call("/auth/token", "POST", b"username=a&password=b",
                                   {"Content-Type": "application/x-www-form-urlencoded"})[0], 200)
        self.assertEqual(self.call("/tenant/keys/x", "DELETE")[0], 204)
        self.assertEqual(self.call("/provider/missing")[0], 404)       # the API's errors come back as they are

    def test_addon_services_pass_through(self):
        code, body, _ = self.call("/addon/containers/web/logs?tail=5", headers={"Authorization": "Bearer t0k"})
        r = json.loads(body)
        self.assertEqual((code, r["path"], r["auth"]), (200, "/addon/containers/web/logs?tail=5", "Bearer t0k"))
        code, body, _ = self.call("/addon/containers", "POST", b'{"name":"web"}', {"Content-Type": "application/json"})
        self.assertEqual((code, json.loads(body)["body"]), (200, '{"name":"web"}'))
        self.assertEqual(json.loads(self.call("/addon/addons")[1]), ["containers"])
        self.assertEqual(self.call("/addon/k8s/clusters")[0], 404)       # not configured: the portal's own 404

    def test_api_down(self):
        self.api_port = self.api.server_port
        p = free_port()
        proc = subprocess.Popen([sys.executable, "portal.py", "--api", f"http://127.0.0.1:{free_port()}",
                                 "--bind", "127.0.0.1", "--port", str(p), "--quiet"])
        try:
            for _ in range(50):
                try: socket.create_connection(("127.0.0.1", p), 0.2).close(); break
                except OSError: time.sleep(0.1)
            req = urllib.request.Request(f"http://127.0.0.1:{p}/auth/me")
            with self.assertRaises(urllib.error.HTTPError) as e: urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 502)
        finally:
            proc.terminate(); proc.wait()


if __name__ == "__main__":
    unittest.main()
