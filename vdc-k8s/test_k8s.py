#!/usr/bin/env python3
"""Tests for vdc_k8s.py, with a stand-in cloud API and a stand-in ssh that plays vdc-node and
node-setup.sh on pretend nodes. Standard library only:

    python3 test_k8s.py          (or: python3 -m unittest -v test_k8s)
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
import vdc_k8s as vk  # noqa: E402

TMP = tempfile.mkdtemp(prefix="vdc-k8s-test-")
NODES = os.path.join(TMP, "nodes")          # one JSON file per pretend node (by address)

FAKE_SSH = r'''#!/usr/bin/env python3
import json, os, re, shlex, sys
NODES = %r
target, remote = sys.argv[-2], sys.argv[-1]
ip = target.split("@")[1]
path = os.path.join(NODES, ip + ".json")
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
    st["env"] = dict(re.findall(r"^export (\w+)=(.*)$", payload, re.M))
    st["env"] = {k: shlex.split(v)[0] if v else "" for k, v in st["env"].items()}
    st["helper"] = "VDC_NODE_EOF" in payload
    if os.path.exists(os.path.join(NODES, "FAIL_SETUP")):
        out(1, "node-setup: can't reach get.k3s.io (internet off?)")
    out(0, "downloading k3s (stable channel)\nok k3s v1.33.5+k3s1")
w = shlex.split(remote)
assert w[:3] == ["doas", "-n", "/usr/local/bin/vdc-node"], remote
w = w[3:]
if w[0] == "token":
    out(0, "K10secret::server:abc")
if w[0] == "kubeconfig":
    out(0, "apiVersion: v1\nclusters:\n- cluster:\n    server: https://127.0.0.1:6443\n  name: default\ncontexts:\n- context:\n    cluster: default\n    user: default\n  name: default\ncurrent-context: default\nusers:\n- name: default\n  user: {}")
if w[0] == "metallb":
    st["metallb"] = w[1:]; out(0, "installing MetalLB %%s\nMetalLB %%s: address pool %%s" %% (w[1], w[1], " ".join(w[2:])))
if w[0] == "services":
    out(0, json.dumps(st.get("services", {"items": []})))
if w[0] == "nodes":
    out(0, "\n".join("%%s\tReady\t%%s\tv1.33.5+k3s1" %% (n, "control-plane" if "cp" in n else "worker") for n in st.get("k8s_nodes", [])))
if w[0] == "kubectl":
    st.setdefault("kubectl", []).append([w[1:], sys.stdin.read() if not sys.stdin.isatty() else ""])
    out(0, "deployment.apps/web created")
if w[0] == "remove":
    st.setdefault("removed", []).append(w[1]); out(0, 'node "%%s" deleted' %% w[1])
out(2, "fake vdc-node: %%s?" %% w)
''' % NODES


def node(ip):
    with open(os.path.join(NODES, ip + ".json")) as f:
        return json.load(f)


def edit_node(ip, **kw):
    st = node(ip)
    st.update(kw)
    with open(os.path.join(NODES, ip + ".json"), "w") as f:
        json.dump(st, f)


# ------------------------------------------------------------------------------- stand-in cloud API
class Kit:
    users = {"tok-a": dict(username="alice", role="consumer", tenant="tenant-a"),
             "tok-b": dict(username="bob", role="consumer", tenant="tenant-b"),
             "tok-p": dict(username="admin", role="provider", tenant=None),
             "tok-au": dict(username="audrey", role="auditor", tenant=None)}
    vms, keys, ports, ips, fips, jobs = {}, {}, {}, {}, {}, {}
    next_ip = {"tenant-a": 10, "tenant-b": 10}
    nets = {"tenant-a": "10.199.20.", "tenant-b": "10.198.20."}


K = Kit()


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
        me = K.users.get(self.headers.get("Authorization", "")[7:])
        if not me:
            return self.ans(401, {"detail": "Not authenticated"})
        n = int(self.headers.get("Content-Length") or 0)
        b = json.loads(self.rfile.read(n)) if n else {}
        p, m, t = self.path, self.command, me["tenant"]
        if p == "/auth/me":
            return self.ans(200, me)
        if p.startswith("/tenant/") and me["role"] != "consumer":
            return self.ans(403, {"detail": "consumers only"})
        if p == "/tenant/keys":
            if m == "POST":
                K.keys.setdefault(t, []).append(dict(id="k1", comment=b["key"].split()[-1]))
                return self.ans(201, K.keys[t][-1])
            return self.ans(200, K.keys.get(t, []))
        if p == "/tenant/vms" and m == "POST":
            if b["name"] in K.vms:
                return self.ans(409, {"detail": f"VM {b['name']} exists"})
            ip = K.nets[t] + str(K.next_ip[t])
            K.next_ip[t] += 1
            K.vms[b["name"]] = dict(name=b["name"], tenant=t, ip=ip, flavor=b["flavor"], image=b["image"])
            with open(os.path.join(NODES, ip + ".json"), "w") as f:
                json.dump({}, f)
            jid = f"vm-create-{len(K.jobs)}"
            K.jobs[jid] = "finished"
            return self.ans(202, {"job": jid})
        if p.startswith("/tenant/jobs/"):
            return self.ans(200, dict(state=K.jobs.get(p.split("/")[-1], "unknown")))
        if p.startswith("/tenant/vms/") or p.startswith("/provider/vms/"):
            v = K.vms.get(p.split("/")[-1])
            if not v or (t and v["tenant"] != t):
                return self.ans(404, {"detail": "no such VM"})
            if m == "DELETE":
                del K.vms[v["name"]]
                jid = f"vm-delete-{len(K.jobs)}"
                K.jobs[jid] = "finished"
                return self.ans(202, {"job": jid})
            return self.ans(200, v)
        if p == "/tenant/access":
            return self.ans(200, dict(jump_host="lab-pc", jump_port=45222, jump_user=f"t-{t}",
                                      guests=[dict(name=v["name"], user="alpine") for v in K.vms.values() if v["tenant"] == t]))
        if p == "/tenant/ips" and m == "POST":
            ip = K.nets[t] + str(200 + len(K.ips))
            K.ips[ip] = dict(tenant=t, **b)
            return self.ans(201, dict(ip=ip, tenant=t))
        if p == "/tenant/ports" and m == "POST":
            if b["target"] not in K.ips:
                return self.ans(409, {"detail": "not an address of the tenant"})
            cp = 47000 + len(K.ports) + sum(1 for _ in K.ports)
            while cp in K.ports:
                cp += 1
            K.ports[cp] = dict(tenant=t, **b)
            return self.ans(201, dict(campus_port=cp, connect=f"<lab PC>:{cp}"))
        if p == "/tenant/floating-ips" and m == "POST":
            if len([f for f in K.fips.values() if f["tenant"] == t]) >= 2:
                return self.ans(403, {"detail": "quota exceeded: floating ips 3/2"})
            f = next(f"10.28.84.{i}" for i in range(200, 250) if f"10.28.84.{i}" not in K.fips)
            K.fips[f] = dict(tenant=t, target=b.get("target"))
            return self.ans(201, dict(ip=f, target=b.get("target")))
        if p.startswith("/tenant/floating-ips/") and p.endswith("/associate"):
            f = p.split("/")[3]
            K.fips[f]["target"] = b["target"]
            return self.ans(200, dict(ip=f, target=b["target"]))
        for prefix, store in (("ports", K.ports), ("ips", K.ips), ("floating-ips", K.fips)):
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


def start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


def lb_service(ns, name, ip, mode, ports=((80, "TCP"),)):
    return {"metadata": {"namespace": ns, "name": name, "annotations": {"vdc/expose": mode} if mode else {}},
            "spec": {"type": "LoadBalancer", "ports": [{"port": p, "protocol": pr} for p, pr in ports]},
            "status": {"loadBalancer": {"ingress": [{"ip": ip}] if ip else []}}}


class K8sService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs(NODES, exist_ok=True)
        ssh = os.path.join(TMP, "fake-ssh")
        with open(ssh, "w") as f:
            f.write(FAKE_SSH)
        os.chmod(ssh, 0o755)
        vk.Conf.api = start(ThreadingHTTPServer(("127.0.0.1", 0), KitHandler))
        vk.Conf.state = os.path.join(TMP, "state")
        vk.Conf.ssh = [sys.executable, ssh]
        vk.ensure_key()
        vk.db()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), vk.Handler)
        srv.quiet = True
        cls.url = start(srv) + vk.PREFIX

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def call(self, method, path, body=None, token="tok-a"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def wait(self, accepted, token="tok-a", ok=True):
        code, a = accepted
        self.assertEqual(code, 202, a)
        for _ in range(400):
            _, j = self.call("GET", f"/jobs/{a['job']}", token=token)
            if j["state"] in ("finished", "failed"):
                if ok:
                    self.assertEqual(j["state"], "finished", j)
                return j
            time.sleep(0.05)
        self.fail("job did not end")

    def ip(self, vm):
        return K.vms[vm]["ip"]

    # -------------------------------------------------------------------------------------
    def test_01_create_a_cluster(self):
        j = self.wait(self.call("POST", "/clusters", {"name": "c1", "workers": 2}))
        self.assertEqual(sorted(K.vms), ["k8s-c1-cp1", "k8s-c1-w1", "k8s-c1-w2"])
        self.assertEqual({(v["flavor"], v["image"]) for v in K.vms.values()}, {("medium", "alpine")})
        self.assertEqual([k["comment"] for k in K.keys["tenant-a"]], ["vdc-k8s"])
        cp = node(self.ip("k8s-c1-cp1"))
        self.assertTrue(cp["helper"])
        self.assertEqual((cp["env"]["ROLE"], cp["env"]["NODE_IP"], cp["env"]["INGRESS"]), ("server", self.ip("k8s-c1-cp1"), "no"))
        for w in ("k8s-c1-w1", "k8s-c1-w2"):
            env = node(self.ip(w))["env"]
            self.assertEqual((env["ROLE"], env["K3S_URL"], env["K3S_TOKEN"], env["NODE_IP"]),
                             ("agent", f"https://{self.ip('k8s-c1-cp1')}:6443", "K10secret::server:abc", self.ip(w)))
        lb = [ip for ip, v in K.ips.items() if v["tenant"] == "tenant-a"]
        self.assertEqual(cp["metallb"], ["v0.16.1"] + lb)
        self.assertEqual(K.ips[lb[0]]["note"], "k8s-c1-lb1")
        _, c = self.call("GET", "/clusters/c1")
        self.assertEqual((c["status"], c["version"], c["lb_ips"], c["workers"]), ("ready", "v1.33.5+k3s1", lb, 2))
        self.assertEqual([(n["name"], n["role"], n["status"]) for n in c["nodes"]],
                         [("k8s-c1-cp1", "server", "ready"), ("k8s-c1-w1", "agent", "ready"), ("k8s-c1-w2", "agent", "ready")])
        self.assertTrue(any("vdc-node metallb" in line for line in j["log"]))
        edit_node(self.ip("k8s-c1-cp1"), k8s_nodes=["k8s-c1-cp1", "k8s-c1-w1", "k8s-c1-w2"])
        _, c = self.call("GET", "/clusters/c1?state=true")
        self.assertEqual(c["nodes"][1]["k8s"], {"status": "Ready", "role": "worker", "version": "v1.33.5+k3s1"})

    def test_02_kubeconfig(self):
        code, k = self.call("GET", "/clusters/c1/kubeconfig")
        self.assertEqual((code, k["server"]), (200, "https://127.0.0.1:6443"))
        self.assertIn(f"ssh -N -L 6443:{self.ip('k8s-c1-cp1')}:6443 -p 45222 t-tenant-a@lab-pc", k["how"])
        self.assertIn("current-context: c1", k["kubeconfig"])
        self.assertIn("server: https://127.0.0.1:6443", k["kubeconfig"])
        k = self.call("GET", "/clusters/c1/kubeconfig?access=internal")[1]
        self.assertIn(f"server: https://{self.ip('k8s-c1-cp1')}:6443", k["kubeconfig"])
        self.assertEqual(self.call("GET", "/clusters/c1/kubeconfig?access=floating")[0], 409)
        self.assertEqual(self.call("GET", "/clusters/c1/kubeconfig?tenant=tenant-a", token="tok-p")[0], 403)

    def test_03_limits_and_bad_input(self):
        code, a = self.call("POST", "/clusters", {"name": "c2"})
        self.assertEqual(code, 409)
        self.assertIn("most allowed (1)", a["detail"])
        self.assertEqual(self.call("POST", "/clusters", {"name": "c1"}, token="tok-b")[0], 409)   # names are global
        for body in ({"name": "C1"}, {"name": "toolongname"}, {"name": "ab-"}, {"name": "x", "workers": 9},
                     {"name": "x", "lb_ips": -1}, {"name": "x", "api": "public"}):
            self.assertEqual(self.call("POST", "/clusters", body, token="tok-b")[0], 422, body)
        self.assertEqual(self.call("POST", "/clusters", {"name": "x"}, token="tok-p")[0], 403)

    def test_04_annotated_services_reach_campus(self):
        lb = self.call("GET", "/clusters/c1")[1]["lb_ips"][0]
        edit_node(self.ip("k8s-c1-cp1"), services={"items": [
            lb_service("shop", "web", lb, "port", ((80, "TCP"), (53, "UDP"))),
            lb_service("shop", "plain", lb, None),                       # no annotation: stays inside
            lb_service("shop", "pending", None, "port")]})               # no address yet: nothing to do
        code, r = self.call("POST", "/clusters/c1/kubectl", {"args": ["apply", "-f", "-"], "stdin": "kind: Service\n"})
        self.assertEqual((code, r["exit_code"]), (200, 0))
        self.assertEqual(node(self.ip("k8s-c1-cp1"))["kubectl"][-1], [["apply", "-f", "-"], "kind: Service\n"])
        self.assertEqual([(e["service"], e["port"], e["protocol"], e["campus_port"] is not None) for e in r["exposures"]],
                         [("web", 53, "udp", True), ("web", 80, "tcp", True)])
        self.assertEqual({(v["target"], v["port"], v["protocol"]) for v in K.ports.values()}, {(lb, 80, "tcp"), (lb, 53, "udp")})
        # nothing changed: the same ports stay
        before = dict(K.ports)
        self.assertEqual(len(self.call("POST", "/clusters/c1/sync")[1]["exposures"]), 2)
        self.assertEqual(K.ports, before)
        # switch to a floating IP: the ports close, the floating IP points at the LoadBalancer address
        edit_node(self.ip("k8s-c1-cp1"), services={"items": [lb_service("shop", "web", lb, "floating")]})
        ex = self.call("POST", "/clusters/c1/sync")[1]["exposures"]
        self.assertEqual(K.ports, {})
        self.assertEqual(len(ex), 1)
        self.assertEqual(K.fips[ex[0]["floating_ip"]]["target"], lb)
        self.assertEqual(self.call("GET", "/clusters/c1/services")[1][0]["expose"], "floating")
        # the annotation removed: the floating IP goes back
        edit_node(self.ip("k8s-c1-cp1"), services={"items": [lb_service("shop", "web", lb, None)]})
        self.assertEqual(self.call("POST", "/clusters/c1/sync")[1]["exposures"], [])
        self.assertEqual(K.fips, {})

    def test_05_a_refused_exposure_is_kept_with_its_error(self):
        lb = self.call("GET", "/clusters/c1")[1]["lb_ips"][0]
        K.fips.update({"10.28.84.240": dict(tenant="tenant-a", target="x"), "10.28.84.241": dict(tenant="tenant-a", target="y")})
        edit_node(self.ip("k8s-c1-cp1"), services={"items": [lb_service("shop", "web", lb, "floating")]})
        ex = self.call("POST", "/clusters/c1/sync")[1]["exposures"]
        self.assertIn("quota exceeded", ex[0]["error"])
        for f in ("10.28.84.240", "10.28.84.241"):
            del K.fips[f]
        ex = self.call("POST", "/clusters/c1/sync")[1]["exposures"]      # tried again on the next sync
        self.assertEqual((ex[0]["error"], ex[0]["floating_ip"] is not None), (None, True))

    def test_06_scale(self):
        self.wait(self.call("PATCH", "/clusters/c1", {"workers": 3}))
        self.assertIn("k8s-c1-w3", K.vms)
        self.assertEqual(node(self.ip("k8s-c1-w3"))["env"]["ROLE"], "agent")
        cp_ip = self.ip("k8s-c1-cp1")
        self.wait(self.call("PATCH", "/clusters/c1", {"workers": 1}))
        self.assertEqual(sorted(v for v in K.vms if v.startswith("k8s-c1")), ["k8s-c1-cp1", "k8s-c1-w1"])
        self.assertEqual(node(cp_ip)["removed"], ["k8s-c1-w3", "k8s-c1-w2"])
        _, c = self.call("GET", "/clusters/c1")
        self.assertEqual((c["workers"], c["status"], len(c["nodes"])), (1, "ready", 2))
        self.assertEqual(self.call("PATCH", "/clusters/c1", {"workers": 7})[0], 422)

    def test_07_tenants_and_roles(self):
        self.assertEqual(self.call("GET", "/clusters", token="tok-b")[1], [])
        self.assertEqual(self.call("GET", "/clusters/c1", token="tok-b")[0], 404)
        self.assertEqual(self.call("DELETE", "/clusters/c1", token="tok-b")[0], 404)
        self.assertEqual([c["name"] for c in self.call("GET", "/clusters", token="tok-p")[1]], ["c1"])
        self.assertEqual(self.call("GET", "/clusters/c1", token="tok-au")[0], 200)
        self.assertEqual(self.call("POST", "/clusters/c1/sync", token="tok-au")[0], 403)
        self.assertEqual(self.call("POST", "/clusters/c1/kubectl", {"args": ["get", "pods"]}, token="tok-p")[0], 403)
        self.assertEqual(self.call("GET", "/clusters", token="nope")[0], 401)

    def test_08_api_on_a_floating_ip(self):
        self.wait(self.call("POST", "/clusters", {"name": "b1", "workers": 0, "lb_ips": 0, "api": "floating", "ingress": True},
                            token="tok-b"), token="tok-b")
        _, c = self.call("GET", "/clusters/b1", token="tok-b")
        fip = c["api_fip"]
        self.assertEqual(K.fips[fip]["target"], "k8s-b1-cp1")
        env = node(self.ip("k8s-b1-cp1"))["env"]
        self.assertEqual((env["TLS_SANS"], env["INGRESS"]), (fip, "yes"))
        self.assertNotIn("metallb", node(self.ip("k8s-b1-cp1")), "no LoadBalancer addresses: no MetalLB")
        k = self.call("GET", "/clusters/b1/kubeconfig?access=floating", token="tok-b")[1]
        self.assertIn(f"server: https://{fip}:6443", k["kubeconfig"])

    def test_09_delete_releases_everything(self):
        lb = self.call("GET", "/clusters/c1")[1]["lb_ips"]
        self.wait(self.call("DELETE", "/clusters/c1"))
        self.assertFalse([v for v in K.vms if v.startswith("k8s-c1")])
        self.assertFalse([ip for ip in lb if ip in K.ips])
        self.assertFalse([f for f, v in K.fips.items() if v["tenant"] == "tenant-a"])
        self.assertEqual(self.call("GET", "/clusters/c1")[0], 404)
        # the provider deletes the other tenant's cluster, through /provider
        self.wait(self.call("DELETE", "/clusters/b1?tenant=tenant-b", token="tok-p"), token="tok-p")
        self.assertEqual((K.vms, K.fips), ({}, {}))

    def test_10_a_failed_setup_can_be_deleted(self):
        flag = os.path.join(NODES, "FAIL_SETUP")            # every node's setup fails while it exists
        open(flag, "w").close()
        try:
            j = self.wait(self.call("POST", "/clusters", {"name": "c3", "workers": 0}), ok=False)
        finally:
            os.remove(flag)
        self.assertIn("internet off", j["error"])
        _, c = self.call("GET", "/clusters/c3")
        self.assertEqual((c["status"], c["nodes"][0]["status"]), ("error", "error"))
        self.assertEqual(self.call("GET", "/clusters/c3/kubeconfig")[0], 409)
        self.wait(self.call("DELETE", "/clusters/c3"))
        self.assertEqual((K.vms, [ip for ip, v in K.ips.items() if v["tenant"] == "tenant-a"]), ({}, []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
