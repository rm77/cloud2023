# python3 test_meter.py   - vdc_meter.py and portal.py's /addon/ against a stand-in API (standard library only)
import json, os, socket, subprocess, sys, tempfile, threading, time, unittest, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import vdc_meter as M

USERS = {"meter": ("meter-pass-1", {"username": "meter", "role": "provider", "tenant": None}),
         "alice": ("alice-pass-1", {"username": "alice", "role": "consumer", "tenant": "tenant-a"}),
         "tari": ("tari-pass-1", {"username": "tari", "role": "auditor", "tenant": None})}
DATA = {
    "/provider/flavors": [{"name": "tiny", "vcpus": 1, "mem_mb": 256, "disk_gb": 2, "price_per_hour": 0.005},
                          {"name": "small", "vcpus": 1, "mem_mb": 512, "disk_gb": 5, "price_per_hour": 0.01}],
    "/provider/vms?state=true": [
        {"name": "a1", "tenant": "tenant-a", "flavor": "small", "vcpus": 1, "mem_mb": 512, "status": "active", "state": "running"},
        {"name": "a2", "tenant": "tenant-a", "flavor": "tiny", "vcpus": 1, "mem_mb": 256, "status": "active", "state": "shut off"},
        {"name": "a3", "tenant": "tenant-a", "flavor": "tiny", "vcpus": 1, "mem_mb": 256, "status": "creating", "state": None},
        {"name": "b1", "tenant": "tenant-b", "flavor": "small", "vcpus": 1, "mem_mb": 512, "status": "active", "state": "running"}],
    "/provider/tenants": [{"name": "tenant-a", "internet": True}, {"name": "tenant-b", "internet": False}],
    "/provider/tunnels": [{"id": "t1", "vm": "a1", "protocol": "http"}, {"id": "t2", "vm": "a1", "protocol": "ssh"},
                          {"id": "t3", "vm": "b1", "protocol": "http"}],
    "/provider/users": [{"username": "alice", "tenant": "tenant-a"}, {"username": "al2", "tenant": "tenant-a"},
                        {"username": "bob", "tenant": "tenant-b"}, {"username": "meter", "tenant": None}],
}


class FakeApi(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def who(self):
        tok = self.headers.get("Authorization", "")[7:]
        return USERS[tok.split(":")[1]][1] if tok.startswith("tok:") and tok.split(":")[1] in USERS else None

    def do_POST(self):
        f = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        u, p = f["username"][0], f["password"][0]
        if u in USERS and USERS[u][0] == p:
            return self.reply(200, {"access_token": f"tok:{u}", "token_type": "bearer"})
        self.reply(401, {"detail": "bad login"})

    def do_GET(self):
        me = self.who()
        if not me:
            return self.reply(401, {"detail": "not logged in"})
        if self.path == "/auth/me":
            return self.reply(200, me)
        if self.path in DATA:
            return self.reply(200 if me["role"] == "provider" else 403, DATA[self.path])
        self.reply(404, {"detail": "nope"})


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class MeterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tmp = tempfile.mkdtemp()
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "metrics.json")) as f:
            cfg = json.load(f)
        cfg["api"] = f"http://127.0.0.1:{cls.srv.server_port}"
        cfg["db"] = os.path.join(cls.tmp, "meter.db")
        for m in cfg["metrics"]:
            m["enabled"] = True                                    # the example command too
        cfg["metrics"] += [
            {"name": "vcpu", "source": "vm", "value": "vcpus", "when": "running", "per": "hour", "price": 0.004},
            {"name": "volume", "source": "command", "kind": "amount", "unit": "GB", "price": 0.05,
             "command": "echo 'tenant-a a1 2.5'; echo '# a comment'; echo 'tenant-b b1 1'"},
            {"name": "broken", "source": "command", "command": "exit 3"}]
        cls.cfg_path = os.path.join(cls.tmp, "metrics.json")
        with open(cls.cfg_path, "w") as f:
            json.dump(cfg, f)
        cls.cfg = M.load_config(cls.cfg_path)
        cls.api = M.Api(cls.cfg["api"], "meter", "meter-pass-1")
        cls.api.login()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def test_1_config_is_checked(self):
        bad = os.path.join(self.tmp, "bad.json")
        for metric in ({"name": "x", "source": "nope"}, {"name": "x", "source": "vm", "value": "gpus"},
                       {"name": "x", "source": "vm", "when": "sometimes"}, {"name": "x y", "source": "tunnels"},
                       {"name": "x", "source": "command"}, {"name": "x", "source": "vm", "price": "free"}):
            with open(bad, "w") as f:
                json.dump({"metrics": [metric]}, f)
            with self.assertRaises(SystemExit):
                M.load_config(bad)

    def test_2_only_a_provider_may_meter(self):
        with self.assertRaises(SystemExit):
            M.Api(self.cfg["api"], "tari", "tari-pass-1").login()
        with self.assertRaises(SystemExit):
            M.Api(self.cfg["api"], "meter", "wrong").login()

    def test_3_one_round(self):
        logs = []
        rows = M.sample_once(self.cfg, self.api, save=False, log=logs.append)
        got = {(r[0], r[4], r[5]): (r[6], r[7], r[9]) for r in rows}
        h = 5 / 60
        # disk: every active VM, running or stopped (a3 is still being created), GB-month
        self.assertEqual(got[("disk", "tenant-a", "a1")][0], 5)
        self.assertEqual(got[("disk", "tenant-a", "a2")][0], 2)
        self.assertNotIn(("disk", "tenant-a", "a3"), got)
        self.assertAlmostEqual(got[("disk", "tenant-a", "a1")][1], 5 * h / 730)
        self.assertAlmostEqual(got[("disk", "tenant-a", "a1")][2], 5 * h / 730 * 0.10)
        # vcpu: running only
        self.assertIn(("vcpu", "tenant-a", "a1"), got)
        self.assertNotIn(("vcpu", "tenant-a", "a2"), got)
        # tunnels: counted per VM and given the VM's tenant
        self.assertEqual(got[("tunnels", "tenant-a", "a1")][0], 2)
        self.assertEqual(got[("tunnels", "tenant-b", "b1")][0], 1)
        # internet: tenant-wide, only where it is on
        self.assertEqual(got[("internet", "tenant-a", "-")][0], 1)
        self.assertNotIn(("internet", "tenant-b", "-"), got)
        # the example command (accounts through the API)
        self.assertEqual(got[("accounts", "tenant-a", "-")][0], 2)
        self.assertEqual(got[("accounts", "tenant-b", "-")][0], 1)
        # an amount is charged as it is, not over time
        self.assertEqual(got[("volume", "tenant-a", "a1")], (2.5, 2.5, 2.5 * 0.05))
        # a failing metric is logged and the rest still counted
        self.assertTrue(any("broken" in x for x in logs))

    def test_4_report_and_periods(self):
        M.sample_once(self.cfg, self.api, log=lambda *_: None)
        M.sample_once(self.cfg, self.api, log=lambda *_: None)
        now = datetime.now(timezone.utc)
        r = M.report(self.cfg, now - timedelta(hours=1), now + timedelta(minutes=1))
        disk_a1 = next(l for l in r["lines"] if (l["metric"], l["vm"]) == ("disk", "a1"))
        self.assertAlmostEqual(disk_a1["cost"], round(2 * 5 * (5 / 60) / 730 * 0.10, 4))
        traffic = next(m for m in r["metrics"] if m["metric"] == "volume")
        self.assertEqual((traffic["quantity"], traffic["unit"]), (7.0, "GB"))
        self.assertAlmostEqual(r["total_cost"], round(sum(l["cost"] for l in r["lines"]), 4), places=3)
        # gauges only in the rate: tunnels 3*0.01 + internet 0.002 + accounts 3/730 + vcpu 2*0.004 + disk 12*0.1/730
        self.assertAlmostEqual(r["rate_per_hour"], 0.03 + 0.002 + 3 / 730 + 0.008 + 12 * 0.1 / 730, places=5)
        ra = M.report(self.cfg, now - timedelta(hours=1), now + timedelta(minutes=1), "tenant-a")
        self.assertEqual({l["tenant"] for l in ra["lines"]}, {"tenant-a"})
        self.assertEqual(M.report(self.cfg, now + timedelta(minutes=2))["lines"], [])     # nothing after now
        self.assertEqual(r["days"][-1]["date"], now.strftime("%Y-%m-%d"))

    def test_5_portal_addon_endpoints(self):
        port = free_port()
        p = subprocess.Popen([sys.executable, "portal.py", "--api", self.cfg["api"], "--meter-config", self.cfg_path,
                              "--bind", "127.0.0.1", "--port", str(port), "--quiet"])
        try:
            for _ in range(50):
                try: socket.create_connection(("127.0.0.1", port), 0.2).close(); break
                except OSError: time.sleep(0.1)
            def get(path, user=None):
                h = {"Authorization": f"Bearer tok:{user}"} if user else {}
                try:
                    with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=h)) as r:
                        return r.status, json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read())
            self.assertEqual(get("/addon/usage")[0], 401)
            code, m = get("/addon/metrics", "alice")
            self.assertEqual(code, 200)
            self.assertTrue(all("command" not in x for x in m["metrics"]))   # commands are not shown
            self.assertNotIn("echo", json.dumps(m))
            code, r = get("/addon/usage?tenant=tenant-b", "alice")      # a consumer sees only her tenant
            self.assertEqual(code, 200)
            self.assertEqual({l["tenant"] for l in r["lines"]}, {"tenant-a"})
            code, r = get("/addon/usage?tenant=tenant-b", "meter")
            self.assertEqual({l["tenant"] for l in r["lines"]}, {"tenant-b"})
            code, r = get("/addon/usage", "tari")                       # the auditor sees all
            self.assertEqual({l["tenant"] for l in r["lines"]}, {"tenant-a", "tenant-b"})
            self.assertEqual(get("/addon/usage?since=yesterday", "meter")[0], 422)
        finally:
            p.terminate(); p.wait()
        port2 = free_port()                                              # without --meter-config: 404
        p = subprocess.Popen([sys.executable, "portal.py", "--api", self.cfg["api"], "--bind", "127.0.0.1",
                              "--port", str(port2), "--quiet"])
        try:
            for _ in range(50):
                try: socket.create_connection(("127.0.0.1", port2), 0.2).close(); break
                except OSError: time.sleep(0.1)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port2}/addon/metrics",
                                                              headers={"Authorization": "Bearer tok:meter"}))
            self.assertEqual(e.exception.code, 404)
        finally:
            p.terminate(); p.wait()


if __name__ == "__main__":
    unittest.main()
