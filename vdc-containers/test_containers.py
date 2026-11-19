#!/usr/bin/env python3
"""Tests for vdc_containers.py, with a stand-in cloud API and a stand-in ssh that plays
vdc-host on pretend container hosts. Standard library only:

    python3 test_containers.py          (or: python3 -m unittest -v test_containers)
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import vdc_containers as vc  # noqa: E402

TMP = tempfile.mkdtemp(prefix="vdc-containers-test-")
HOSTS = os.path.join(TMP, "hosts")          # one JSON file per pretend host (by address)

# ------------------------------------------------------------------------------- stand-in ssh
FAKE_SSH = r'''#!/usr/bin/env python3
import json, os, shlex, sys
HOSTS = %r
args = sys.argv[1:]
target, remote = args[-2], args[-1]
user, ip = target.split("@")
path = os.path.join(HOSTS, ip + ".json")
if not os.path.exists(path) or json.load(open(path)).get("down"):
    print("ssh: connect to host %%s port 22: No route to host" %% ip, file=sys.stderr); sys.exit(255)
st = json.load(open(path))
st.setdefault("calls", []).append(remote)
def save(): json.dump(st, open(path, "w"))
def out(code, text=""):
    save()
    if text: print(text)
    sys.exit(code)
if remote.startswith("id -u"):
    out(0, "1000\n/usr/bin/doas")
if remote == "doas -n true":
    out(0)
if remote == "doas -n sh -s":
    payload = sys.stdin.read()
    st["setup"] = "VDC_HOST_EOF" in payload and "host-setup" in payload
    out(0, "installing docker and docker compose (apk)\nok docker 27.3.1")
w = shlex.split(remote)
assert w[:3] == ["doas", "-n", "/usr/local/bin/vdc-host"], remote
w = w[3:]
c = st.setdefault("containers", {})
if w[0] == "run":
    name = w[w.index("--name") + 1]
    i, image = 1, None
    while i < len(w):            # options with a value, then the image
        if w[i].startswith("-"):
            i += 2
            continue
        image = w[i]; break
    if "nosuchimage" in image:
        out(125, "docker: Error response from daemon: pull access denied for %%s" %% image)
    if name in c:
        out(125, 'docker: Error response from daemon: Conflict. The container name "/%%s" is already in use' %% name)
    c[name] = {"state": "running", "argv": w}
    out(0, "f00d" * 16)
if w[0] == "rm":
    c.pop(w[1], None); out(0)
if w[0] in ("start", "restart"):
    c[w[1]]["state"] = "running"; out(0)
if w[0] == "stop":
    c[w[1]]["state"] = "exited"; out(0)
if w[0] == "logs":
    out(0, "2026-11-19T05:00:00Z %%s says hello" %% w[1])
if w[0] == "exec":
    out(0 if w[2] != "false" else 1, " ".join(w[2:]))
if w[0] == "ps":
    out(0, "\n".join("%%s\t%%s\tUp" %% (n, v["state"]) for n, v in c.items()))
if w[0] == "addr":
    a = st.setdefault("addrs", [])
    if w[1] == "add": a.append(w[2])
    if w[1] == "del": st["addrs"] = [x for x in a if x.split("/")[0] != w[2].split("/")[0]]
    out(0)
if w[0] == "stack":
    s = st.setdefault("stacks", {})
    if w[1] == "up":
        s[w[2]] = sys.stdin.read(); out(0, "Container %%s-web-1  Started" %% w[2])
    if w[1] == "down":
        s.pop(w[2], None); out(0)
    if w[1] == "ps":
        out(0, "%%s-web-1\trunning\tUp" %% w[2])
    if w[1] == "logs":
        out(0, "web-1 | ready")
out(2, "fake vdc-host: %%s?" %% w)
''' % HOSTS


def host_state(ip):
    with open(os.path.join(HOSTS, ip + ".json")) as f:
        return json.load(f)


# ------------------------------------------------------------------------------- stand-in cloud API
class Kit:
    users = {"tok-a": dict(username="alice", role="consumer", tenant="tenant-a"),
             "tok-b": dict(username="bob", role="consumer", tenant="tenant-b"),
             "tok-p": dict(username="admin", role="provider", tenant=None),
             "tok-au": dict(username="audrey", role="auditor", tenant=None)}
    flavors = [dict(name="tiny", vcpus=1, mem_mb=256, disk_gb=2), dict(name="medium", vcpus=2, mem_mb=1024, disk_gb=10)]

    def __init__(self):
        self.reset()

    def reset(self):
        self.vms, self.keys, self.ports, self.ips, self.fips, self.jobs, self.calls = {}, {}, {}, {}, {}, {}, []
        self.next_ip = {"tenant-a": 10, "tenant-b": 10}


KIT = Kit()


class KitHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def ans(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def route(self):
        me = KIT.users.get(self.headers.get("Authorization", "")[7:])
        if not me:
            return self.ans(401, {"detail": "Not authenticated"})
        n = int(self.headers.get("Content-Length") or 0)
        b = json.loads(self.rfile.read(n)) if n else {}
        p, m, t = self.path, self.command, me["tenant"]
        KIT.calls.append((me["username"], m, p, b))
        if p == "/auth/me":
            return self.ans(200, me)
        if p.startswith("/tenant/") and me["role"] != "consumer":
            return self.ans(403, {"detail": "consumers only"})
        if p == "/tenant/me":
            return self.ans(200, dict(name=t, subnet="10.199.20.0/24" if t == "tenant-a" else "10.198.20.0/24"))
        if p == "/tenant/flavors":
            return self.ans(200, KIT.flavors)
        if p == "/tenant/keys":
            if m == "POST":
                KIT.keys.setdefault(t, []).append(dict(id="k1", fingerprint="SHA256:x", type="ssh-ed25519",
                                                       comment=b["key"].split()[-1]))
                return self.ans(201, KIT.keys[t][-1])
            return self.ans(200, KIT.keys.get(t, []))
        if p == "/tenant/vms" and m == "POST":
            if b["name"] in KIT.vms:
                return self.ans(409, {"detail": f"VM {b['name']} exists"})
            net = "10.199.20." if t == "tenant-a" else "10.198.20."
            ip = net + str(KIT.next_ip[t])
            KIT.next_ip[t] += 1
            KIT.vms[b["name"]] = dict(name=b["name"], tenant=t, ip=ip, flavor=b["flavor"], image=b["image"], status="active")
            with open(os.path.join(HOSTS, ip + ".json"), "w") as f:
                json.dump({}, f)
            jid = f"vm-create-{len(KIT.jobs)}"
            KIT.jobs[jid] = "finished"
            return self.ans(202, {"job": jid, "status_url": f"/tenant/jobs/{jid}"})
        if p.startswith("/tenant/jobs/"):
            return self.ans(200, dict(id=p.split("/")[-1], state=KIT.jobs.get(p.split("/")[-1], "unknown")))
        if p.startswith("/tenant/vms/") or p.startswith("/provider/vms/"):
            v = KIT.vms.get(p.split("/")[-1])
            if not v or (t and v["tenant"] != t):
                return self.ans(404, {"detail": "no such VM"})
            if m == "DELETE":
                del KIT.vms[v["name"]]
                jid = f"vm-delete-{len(KIT.jobs)}"
                KIT.jobs[jid] = "finished"
                return self.ans(202, {"job": jid})
            return self.ans(200, v)
        if p == "/tenant/access":
            return self.ans(200, {"guests": [dict(name=v["name"], user="alpine") for v in KIT.vms.values() if v["tenant"] == t]})
        if p == "/tenant/ports" and m == "POST":
            cp = 47000 + len(KIT.ports)
            KIT.ports[cp] = dict(tenant=t, **b)
            return self.ans(201, dict(campus_port=cp, connect=f"<lab PC>:{cp}", **b))
        if p == "/tenant/ips" and m == "POST":
            ip = f"10.199.20.{200 + len(KIT.ips)}"
            KIT.ips[ip] = dict(tenant=t, **b)
            return self.ans(201, dict(ip=ip, tenant=t))
        if p == "/tenant/floating-ips" and m == "POST":
            if b.get("target") not in KIT.ips:
                return self.ans(409, {"detail": "not an address of the tenant"})
            f = f"10.28.84.{200 + len(KIT.fips)}"
            KIT.fips[f] = dict(tenant=t, target=b.get("target"))
            return self.ans(201, dict(ip=f, tenant=t, target=b.get("target"), address=b.get("target")))
        for prefix, store in (("ports", KIT.ports), ("ips", KIT.ips), ("floating-ips", KIT.fips)):
            for side in ("/tenant/", "/provider/"):
                if p.startswith(side + prefix + "/") and m == "DELETE":
                    k = p.split("/")[-1]
                    k = int(k) if prefix == "ports" else k
                    if k not in store or (t and store[k]["tenant"] != t):
                        return self.ans(404, {"detail": "not found"})
                    del store[k]
                    return self.ans(204)
        return self.ans(404, {"detail": f"stand-in API: no {m} {p}"})

    do_GET = do_POST = do_DELETE = route


# ------------------------------------------------------------------------------- the tests
def start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


class ContainerService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(HOSTS, exist_ok=True)
        ssh = os.path.join(TMP, "fake-ssh")
        with open(ssh, "w") as f:
            f.write(FAKE_SSH)
        os.chmod(ssh, 0o755)
        os.environ["VDC_CONTAINERS_SSH"] = f"{sys.executable} {ssh}"
        api = start(ThreadingHTTPServer(("127.0.0.1", 0), KitHandler))
        vc.main_args = ["--api", api, "--state", os.path.join(TMP, "state"), "--max-hosts", "2"]
        # what main() sets up, without serving forever
        vc.Conf.api, vc.Conf.state, vc.Conf.max_hosts = api, os.path.join(TMP, "state"), 2
        vc.Conf.ssh = [sys.executable, ssh]
        vc.ensure_key()
        vc.db()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), vc.Handler)
        srv.quiet = True
        cls.url = start(srv) + vc.PREFIX

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def call(self, method, path, body=None, token="tok-a"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def wait(self, accepted, token="tok-a", ok=True):
        code, a = accepted
        self.assertEqual(code, 202, a)
        for _ in range(200):
            _, j = self.call("GET", f"/jobs/{a['job']}", token=token)
            if j["state"] in ("finished", "failed"):
                if ok:
                    self.assertEqual(j["state"], "finished", j)
                return j
            time.sleep(0.05)
        self.fail("job did not end")

    # -------------------------------------------------------------------------------------
    def test_01_first_container_makes_a_host(self):
        j = self.wait(self.call("POST", "", {"name": "web", "image": "nginx:alpine", "mem_mb": 256, "cpus": 0.5,
                                             "ports": [80], "env": {"GREETING": "hi there"}}))
        vm = KIT.vms["ctr-tenant-a-1"]
        self.assertEqual((vm["flavor"], vm["image"]), ("medium", "alpine"))
        self.assertEqual([k["comment"] for k in KIT.keys["tenant-a"]], ["vdc-containers"])
        st = host_state(vm["ip"])
        self.assertTrue(st["setup"], "vdc-host and host-setup.sh were sent")
        argv = st["containers"]["web"]["argv"]
        for a in ("--cpus", "0.5", "--memory", "256m", "-e", "GREETING=hi there", "-p", "20000:80/tcp",
                  "--restart", "unless-stopped", "vdc.managed=1", "vdc.tenant=tenant-a", "nginx:alpine"):
            self.assertIn(a, argv)
        self.assertTrue(any("vdc-host run" in line for line in j["log"]), j["log"])
        code, c = self.call("GET", "/web")
        self.assertEqual((code, c["status"], c["host"], c["address"]), (200, "running", "ctr-tenant-a-1", vm["ip"]))
        self.assertEqual(c["ports"], [{"port": 80, "protocol": "tcp", "host_port": 20000}])

    def test_02_the_next_one_shares_the_host(self):
        self.wait(self.call("POST", "", {"name": "worker", "image": "alpine:3.20", "command": ["sleep", "infinity"],
                                         "mem_mb": 128}))
        self.assertEqual(len(KIT.vms), 1)
        _, c = self.call("GET", "/worker")
        self.assertEqual(c["host"], "ctr-tenant-a-1")
        self.assertEqual(host_state(c["address"])["containers"]["worker"]["argv"][-2:], ["sleep", "infinity"])
        _, hosts = self.call("GET", "/hosts")
        self.assertEqual(hosts[0]["free_mb"], 1024 - 256 - 256 - 128)
        self.assertEqual(sorted(hosts[0]["containers"]), ["web", "worker"])

    def test_03_campus_ports(self):
        self.wait(self.call("POST", "", {"name": "api", "image": "ghcr.io/example/api:1.2", "mem_mb": 64,
                                         "expose": "ports", "ports": [8080, {"port": 5353, "protocol": "udp"}]}))
        _, c = self.call("GET", "/api")
        self.assertEqual([(p["host_port"], p["campus_port"]) for p in c["ports"]], [(20001, 47000), (20002, 47001)])
        self.assertEqual(KIT.ports[47000], dict(tenant="tenant-a", target="ctr-tenant-a-1", port=20001, protocol="tcp"))
        self.assertEqual(KIT.ports[47001]["protocol"], "udp")
        self.assertEqual(c["ports"][0]["connect"], "<lab PC>:47000")

    def test_04_floating_ip_gets_its_own_address(self):
        self.wait(self.call("POST", "", {"name": "game", "image": "itzg/minecraft-server", "mem_mb": 128,
                                         "expose": "floating", "ports": [25565]}))
        _, c = self.call("GET", "/game")
        self.assertEqual((c["address"], c["floating_ip"]), ("10.199.20.200", "10.28.84.200"))
        st = host_state(KIT.vms["ctr-tenant-a-1"]["ip"])
        self.assertIn("10.199.20.200/24", st["addrs"])
        self.assertIn("10.199.20.200:25565:25565/tcp", st["containers"]["game"]["argv"])
        self.assertEqual(KIT.fips["10.28.84.200"]["target"], "10.199.20.200")
        self.wait(self.call("DELETE", "/game"))
        self.assertEqual((KIT.fips, KIT.ips), ({}, {}))
        self.assertNotIn("game", host_state(KIT.vms["ctr-tenant-a-1"]["ip"])["containers"])
        self.assertEqual(host_state(KIT.vms["ctr-tenant-a-1"]["ip"])["addrs"], [])

    def test_05_too_big_for_a_host_and_the_host_limit(self):
        code, _ = self.call("POST", "", {"name": "huge", "image": "x", "mem_mb": 900})
        j = self.wait((code, _), ok=False)
        self.assertEqual(j["state"], "failed")
        self.assertIn("more than a container host has", j["error"])
        self.wait(self.call("POST", "", {"name": "db", "image": "postgres:16", "mem_mb": 700}))
        _, c = self.call("GET", "/db")
        self.assertEqual(c["host"], "ctr-tenant-a-2")                # did not fit on the first
        j = self.wait(self.call("POST", "", {"name": "cache", "image": "redis", "mem_mb": 500}), ok=False)
        self.assertIn("most hosts allowed (2)", j["error"])
        _, c = self.call("GET", "/cache")
        self.assertEqual(c["status"], "error")
        self.wait(self.call("DELETE", "/cache"))
        self.wait(self.call("DELETE", "/huge"))

    def test_06_a_failing_image(self):
        j = self.wait(self.call("POST", "", {"name": "broken", "image": "nosuchimage:1", "mem_mb": 64,
                                             "expose": "ports", "ports": [80]}), ok=False)
        self.assertIn("pull access denied", j["error"])
        _, c = self.call("GET", "/broken")
        self.assertEqual(c["status"], "error")
        self.assertEqual(len(KIT.ports), 2, "no campus port opened for it")
        self.wait(self.call("DELETE", "/broken"))
        self.assertEqual(self.call("GET", "/broken")[0], 404)

    def test_07_bad_input(self):
        for body, word in ((dict(name="Web", image="nginx"), "name"), (dict(name="hosts", image="nginx"), "name"),
                           (dict(name="x1", image="nginx; rm -rf /"), "image"),
                           (dict(name="x1", image="nginx", env={"A-B": "1"}), "env"),
                           (dict(name="x1", image="nginx", ports=[70000]), "ports"),
                           (dict(name="x1", image="nginx", expose="ports"), "needs ports"),
                           (dict(name="x1", image="nginx", mem_mb=10), "mem_mb"),
                           (dict(name="web", image="nginx"), "exists")):
            code, a = self.call("POST", "", body)
            self.assertIn(code, (409, 422), body)
            self.assertIn(word, a["detail"])

    def test_08_logs_exec_power_state(self):
        code, a = self.call("GET", "/web/logs?tail=10")
        self.assertEqual((code, a["logs"]), (200, "2026-11-19T05:00:00Z web says hello"))
        code, a = self.call("POST", "/web/exec", {"command": ["cat", "/etc/os-release"]})
        self.assertEqual((code, a["exit_code"], a["output"]), (200, 0, "cat /etc/os-release"))
        self.assertEqual(self.call("POST", "/web/exec", {"command": "false"})[1]["exit_code"], 1)
        self.assertEqual(self.call("POST", "/web/stop")[1]["status"], "stopped")
        _, lst = self.call("GET", "?state=true")
        self.assertEqual({c["name"]: c["state"] for c in lst}["web"], "exited")
        self.assertEqual(self.call("POST", "/web/start")[1]["status"], "running")
        self.assertEqual(self.call("GET", "/web?state=true")[1]["state"], "running")

    def test_09_tenants_and_roles(self):
        self.assertEqual(self.call("GET", "", token="tok-b")[1], [])
        self.assertEqual(self.call("GET", "/web", token="tok-b")[0], 404)
        self.assertEqual(self.call("DELETE", "/web", token="tok-b")[0], 404)
        self.assertEqual(self.call("GET", "/hosts", token="tok-b")[1], [])
        self.assertEqual(self.call("GET", "", token="nope")[0], 401)
        names = {c["name"] for c in self.call("GET", "", token="tok-p")[1]}
        self.assertTrue({"web", "worker", "api", "db"} <= names)
        self.assertEqual(self.call("GET", "/web", token="tok-au")[0], 200)
        self.assertEqual(self.call("POST", "/web/stop", token="tok-au")[0], 403)
        self.assertEqual(self.call("POST", "", {"name": "p1", "image": "nginx"}, token="tok-p")[0], 403)
        self.assertEqual(self.call("POST", "/web/exec", {"command": ["id"]}, token="tok-p")[0], 403)

    def test_10_stacks(self):
        compose = "services:\n  web:\n    image: nginx\n    ports: ['8081:80']\n"
        self.wait(self.call("POST", "/stacks", {"name": "shop", "compose": compose, "mem_mb": 128,
                                                "expose": "ports", "ports": [8081]}))
        _, s = self.call("GET", "/stacks/shop?state=true")
        self.assertEqual((s["status"], s["host"]), ("running", "ctr-tenant-a-1"))
        self.assertEqual(s["services"], [{"name": "shop-web-1", "state": "running", "status": "Up"}])
        self.assertEqual(host_state(KIT.vms[s["host"]]["ip"])["stacks"]["shop"], compose)
        self.assertEqual(KIT.ports[s["ports"][0]["campus_port"]]["port"], 8081)
        self.assertEqual(self.call("GET", "/stacks/shop/logs")[1]["logs"], "web-1 | ready")
        code, a = self.call("POST", "/stacks", {"name": "shop2", "compose": compose, "mem_mb": 128,
                                                "expose": "ports", "ports": [8081]})
        j = self.wait((code, a), ok=False)               # only fits on the first host, where 8081 is taken
        self.assertIn("8081", j["error"])
        self.wait(self.call("DELETE", "/stacks/shop2"))
        cp = s["ports"][0]["campus_port"]
        self.wait(self.call("DELETE", "/stacks/shop"))
        self.assertNotIn(cp, KIT.ports)
        self.assertNotIn("shop", host_state(KIT.vms[s["host"]]["ip"])["stacks"])

    def test_11_provider_cleans_up_and_hosts_go(self):
        code, a = self.call("DELETE", "/hosts/ctr-tenant-a-2")
        self.assertEqual(code, 409)
        self.assertIn("db", a["detail"])
        self.wait(self.call("DELETE", "/api?tenant=tenant-a", token="tok-p"), token="tok-p")
        self.assertEqual(KIT.ports, {}, "the provider closed the campus ports through /provider/ports")
        for n in ("web", "worker", "db"):
            self.wait(self.call("DELETE", f"/{n}"))
        for h in ("ctr-tenant-a-1", "ctr-tenant-a-2"):
            self.wait(self.call("DELETE", f"/hosts/{h}"))
        self.assertEqual((KIT.vms, self.call("GET", "/hosts")[1]), ({}, []))

    def test_12_an_unreachable_host(self):
        self.wait(self.call("POST", "", {"name": "lone", "image": "nginx", "mem_mb": 64}, token="tok-b"), token="tok-b")
        _, c = self.call("GET", "/lone", token="tok-b")
        path = os.path.join(HOSTS, c["address"] + ".json")
        st = host_state(c["address"])
        st["down"] = True
        with open(path, "w") as f:
            json.dump(st, f)
        self.assertEqual(self.call("GET", "/lone?state=true", token="tok-b")[1]["state"], "unknown")
        self.assertEqual(self.call("POST", "/lone/stop", token="tok-b")[0], 502)
        j = self.wait(self.call("DELETE", "/lone", token="tok-b"), token="tok-b")
        self.assertTrue(any("did not answer" in line for line in j["log"]))
        self.assertEqual(self.call("GET", "/lone", token="tok-b")[0], 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
