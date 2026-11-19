#!/usr/bin/env python3
"""vdc-k8s: a Kubernetes service for vdc-cluster, as an add-on that uses the cloud API.

A tenant asks for a cluster and gets k3s on VMs of its own: one control-plane VM
(k8s-<cluster>-cp1) and workers (k8s-<cluster>-w1, -w2, ...), created through the API with the
caller's own token, so quota, ownership and VM-hour billing apply as for any VM. The add-on
sets the nodes up over SSH (node/node-setup.sh, node/vdc-node), installs MetalLB with extra
addresses of the tenant subnet as its pool, and opens LoadBalancer Services to campus on a
tenant port or a floating IP when they carry the annotation vdc/expose. The kit is not changed.

    python3 vdc_k8s.py --api http://127.0.0.1:36100        # on the main PC
    python3 vdc_k8s.py --check

Every request carries the caller's API token (Authorization: Bearer ...); portal.py passes
/addon/k8s on to this service (--addon k8s=http://...:8092). Python 3.8+ standard library only.
It must run where the tenant subnets are routed (the main PC), as any user with `ssh`.
"""
import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PREFIX = "/addon/k8s"
CLUSTER = re.compile(r"^[a-z](?:[a-z0-9-]{0,6}[a-z0-9])?$")       # VM names: k8s-<cluster>-w12 <= 16
ANNOTATION = "vdc/expose"


class Conf:
    api = "http://127.0.0.1:36100"
    state = os.path.join(HERE, "state")
    node_flavor = "medium"
    node_image = "alpine"
    max_clusters = 1
    max_workers = 3
    k3s_channel = "stable"
    k3s_version = ""
    metallb = "v0.16.1"
    ssh = ["ssh"]
    timeout_vm = 900
    timeout_ssh = 600


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Fail(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code, self.detail = code, detail


# ------------------------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS clusters (name TEXT PRIMARY KEY, tenant TEXT, workers INTEGER, flavor TEXT, image TEXT,
    lb_ips TEXT, api TEXT, api_fip TEXT, ingress INTEGER, version TEXT, status TEXT, error TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS nodes (name TEXT PRIMARY KEY, cluster TEXT, tenant TEXT, role TEXT, ip TEXT, user TEXT,
    root TEXT, status TEXT, error TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS exposures (cluster TEXT, namespace TEXT, service TEXT, mode TEXT, lb_ip TEXT,
    port INTEGER, protocol TEXT, campus_port INTEGER, connect TEXT, floating_ip TEXT, error TEXT,
    PRIMARY KEY (cluster, namespace, service, mode, port, protocol));
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, tenant TEXT, username TEXT, kind TEXT, target TEXT,
    state TEXT, log TEXT, error TEXT, created TEXT, ended TEXT);
"""
_db_lock = threading.RLock()
_db = None


def db():
    global _db
    if _db is None:
        os.makedirs(Conf.state, exist_ok=True)
        _db = sqlite3.connect(os.path.join(Conf.state, "k8s.db"), check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.executescript(SCHEMA)
        _db.execute("UPDATE jobs SET state='failed', error='interrupted: the service stopped', ended=? "
                    "WHERE state IN ('queued','started')", (now(),))
        _db.commit()
    return _db


def q(sql, args=()):
    with _db_lock:
        return [dict(r) for r in db().execute(sql, args).fetchall()]


def x(sql, args=()):
    with _db_lock:
        db().execute(sql, args)
        db().commit()


# ------------------------------------------------------------------------------- the cloud API
class Kit:
    def __init__(self, token, role="consumer"):
        self.token, self.side = token, "/provider" if role == "provider" else "/tenant"

    def call(self, method, path, body=None, ok404=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(Conf.api + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if ok404 and e.code == 404:
                return None
            try:
                detail = json.loads(e.read()).get("detail")
            except Exception:
                detail = None
            raise Fail(e.code if e.code in (401, 403, 404, 409, 422) else 502,
                       f"cloud API {method} {path}: {detail or e.reason}")
        except (urllib.error.URLError, OSError) as e:
            raise Fail(502, f"cloud API at {Conf.api} does not answer ({getattr(e, 'reason', e)})")

    def wait_job(self, job_id, limit):
        end = time.time() + limit
        while time.time() < end:
            j = self.call("GET", f"/tenant/jobs/{job_id}")
            if j["state"] == "finished":
                return j
            if j["state"] in ("failed", "unknown"):
                raise Fail(409, f"job {job_id}: {j.get('error') or j['state']}")
            time.sleep(3)
        raise Fail(504, f"job {job_id} did not finish in {limit} s")


_who = {}


def whoami(token):
    hit = _who.get(token)
    if hit and hit[0] > time.time():
        return hit[1]
    me = Kit(token).call("GET", "/auth/me")
    if len(_who) > 1000:
        _who.clear()
    _who[token] = (time.time() + 60, me)
    return me


# ------------------------------------------------------------------------------- SSH to the nodes
def key_path():
    return os.path.join(Conf.state, "id_k8s")


def ensure_key():
    k = key_path()
    if not os.path.exists(k):
        os.makedirs(Conf.state, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "vdc-k8s", "-f", k], check=True)
    with open(k + ".pub") as f:
        return f.read().strip()


def ssh(node, args, stdin=None, timeout=300, log=None, check=True):
    """run `vdc-node ARGS` (or a raw command when args is a str) on a node, as root"""
    if isinstance(args, str):
        remote = args
    else:
        remote = " ".join(shlex.quote(str(a)) for a in ["/usr/local/bin/vdc-node", *args])
        if node.get("root"):
            remote = node["root"] + " " + remote
    argv = Conf.ssh + ["-i", key_path(), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                       "-o", "StrictHostKeyChecking=accept-new",
                       "-o", "UserKnownHostsFile=" + os.path.join(Conf.state, "known_hosts"),
                       f"{node['user']}@{node['ip']}", remote]
    if log:
        log(f"{node['name']}$ {remote}")
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Fail(504, f"{node['name']}: no answer in {timeout} s")
    out = (p.stdout + p.stderr).strip()
    if check and p.returncode != 0:
        raise Fail(409 if p.returncode != 255 else 502,
                   f"{node['name']}: " + (out.splitlines()[-1] if out else f"ssh exit {p.returncode}"))
    return out if check else (p.returncode, out)


def forget_host_key(ip):
    kh = os.path.join(Conf.state, "known_hosts")
    if ip and os.path.exists(kh):
        subprocess.run(["ssh-keygen", "-R", ip, "-f", kh], capture_output=True)


def setup_payload(env):
    with open(os.path.join(HERE, "node", "vdc-node")) as f:
        helper = f.read()
    with open(os.path.join(HERE, "node", "node-setup.sh")) as f:
        setup = f.read()
    exports = "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in env.items() if v not in (None, ""))
    return ("set -e\nmkdir -p /usr/local/bin\ncat >/usr/local/bin/vdc-node <<'VDC_NODE_EOF'\n" + helper +
            "VDC_NODE_EOF\nchmod 755 /usr/local/bin/vdc-node\n" + exports + setup)


# ------------------------------------------------------------------------------- jobs
def new_job(me, kind, target, tenant=None):
    jid = f"{kind}-{uuid.uuid4().hex[:12]}"
    x("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
      (jid, tenant or me.get("tenant"), me["username"], kind, target, "queued", "", None, now(), None))
    return jid


def run_job(jid, fn, *args):
    def logger(line):
        with _db_lock:
            r = db().execute("SELECT log FROM jobs WHERE id=?", (jid,)).fetchone()
            db().execute("UPDATE jobs SET log=? WHERE id=?", ((r["log"] if r else "") + f"{now()} {line}\n", jid))
            db().commit()

    def body():
        x("UPDATE jobs SET state='started' WHERE id=?", (jid,))
        try:
            fn(logger, *args)
            x("UPDATE jobs SET state='finished', ended=? WHERE id=?", (now(), jid))
        except Exception as e:                       # noqa: BLE001
            msg = e.detail if isinstance(e, Fail) else f"{e.__class__.__name__}: {e}"
            logger("failed: " + msg)
            x("UPDATE jobs SET state='failed', error=?, ended=? WHERE id=?", (msg, now(), jid))
    threading.Thread(target=body, name=jid, daemon=True).start()
    return {"job": jid, "status_url": f"{PREFIX}/jobs/{jid}"}


def job_out(j):
    return dict(j, log=j["log"].splitlines() if j.get("log") else [], status_url=f"{PREFIX}/jobs/{j['id']}")


# ------------------------------------------------------------------------------- nodes
def node_name(cluster, role, n):
    return f"k8s-{cluster}-{'cp' if role == 'server' else 'w'}{n}"


def nodes_of(cluster, role=None):
    rows = q("SELECT * FROM nodes WHERE cluster=?" + (" AND role=?" if role else "") + " ORDER BY name",
             (cluster, role) if role else (cluster,))
    return sorted(rows, key=lambda n: (n["role"] != "server", int(re.sub(r"\D", "", n["name"].rsplit("-", 1)[1]) or 0)))


def server_of(cluster):
    s = nodes_of(cluster, "server")
    if not s or s[0]["status"] != "ready":
        raise Fail(409, f"cluster {cluster}: the control plane is not ready")
    return s[0]


def ensure_tenant_key(kit, log):
    pub = ensure_key()
    if not any(k.get("comment") == "vdc-k8s" for k in kit.call("GET", "/tenant/keys")):
        log("adding the add-on's SSH key to the tenant (POST /tenant/keys): new VMs accept it")
        kit.call("POST", "/tenant/keys", {"key": pub})


def create_vms(kit, c, specs, log):
    """POST /tenant/vms for each (name, role), then wait for all: returns their node rows"""
    jobs = []
    for name, role in specs:
        log(f"VM {name} ({'control plane' if role == 'server' else 'worker'}): POST /tenant/vms flavor {c['flavor']}, image {c['image']}")
        x("INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?)",
          (name, c["name"], c["tenant"], role, None, None, None, "creating", None, now()))
        try:
            jobs.append((name, kit.call("POST", "/tenant/vms", {"name": name, "flavor": c["flavor"], "image": c["image"]})["job"]))
        except Fail as e:
            x("UPDATE nodes SET status='error', error=? WHERE name=?", (e.detail, name))
            raise
    for name, jid in jobs:
        try:
            kit.wait_job(jid, Conf.timeout_vm)
        except Fail as e:
            x("UPDATE nodes SET status='error', error=? WHERE name=?", (e.detail, name))
            raise
    users = {g["name"]: g["user"] for g in kit.call("GET", "/tenant/access")["guests"]}
    out = []
    for name, _ in specs:
        vm = kit.call("GET", f"/tenant/vms/{name}")
        x("UPDATE nodes SET ip=?, user=?, status='setting-up' WHERE name=?", (vm["ip"], users.get(name) or "alpine", name))
        forget_host_key(vm["ip"])
        out.append(q("SELECT * FROM nodes WHERE name=?", (name,))[0])
        log(f"{name}: {vm['ip']}")
    return out


def become_root(n, log):
    log(f"{n['name']}: waiting for SSH as {n['user']}")
    end = time.time() + Conf.timeout_ssh
    while True:
        code, out = ssh(n, "id -u; command -v doas || true; command -v sudo || true", timeout=30, check=False)
        if code == 0:
            break
        if time.time() > end:
            raise Fail(504, f"{n['name']}: no SSH in {Conf.timeout_ssh} s ({out.splitlines()[-1] if out else ''})")
        time.sleep(10)
    words = out.split()
    root = ""
    if words and words[0] != "0":
        for cand in ("doas -n", "sudo -n"):
            if any(w.endswith("/" + cand.split()[0]) for w in words) and ssh(n, cand + " true", timeout=30, check=False)[0] == 0:
                root = cand
                break
        else:
            raise Fail(409, f"{n['name']}: {n['user']} can't become root without a password (doas or sudo)")
    n["root"] = root
    x("UPDATE nodes SET root=? WHERE name=?", (root, n["name"]))


def setup_node(n, env, log):
    become_root(n, log)
    log(f"{n['name']}: installing k3s {'server' if env['ROLE'] == 'server' else 'agent'} (node-setup.sh)")
    try:
        out = ssh(n, (n["root"] + " sh -s").strip(), stdin=setup_payload(env), timeout=1200)
    except Fail as e:
        x("UPDATE nodes SET status='error', error=? WHERE name=?", (e.detail, n["name"]))
        raise
    for line in out.splitlines()[-3:]:
        log(f"{n['name']}: {line}")
    x("UPDATE nodes SET status='ready', error=NULL WHERE name=?", (n["name"],))
    n["status"] = "ready"
    return out.splitlines()[-1] if out else ""


def join_workers(c, server, workers, log):
    token = ssh(server, ["token"]).strip()
    url = f"https://{server['ip']}:6443"
    errors, threads = [], []

    def one(w):
        try:
            setup_node(w, dict(ROLE="agent", NODE_IP=w["ip"], K3S_URL=url, K3S_TOKEN=token,
                               K3S_CHANNEL=Conf.k3s_channel, K3S_VERSION=c["version"] or Conf.k3s_version), log)
        except Fail as e:
            errors.append(e)
    for w in workers:
        t = threading.Thread(target=one, args=(w,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    if errors:
        raise errors[0]


# ------------------------------------------------------------------------------- jobs: create, scale, delete
def check_cluster(b):
    name = b.get("name", "")
    if not isinstance(name, str) or not CLUSTER.match(name):
        raise Fail(422, "name: 1-8 of a-z, 0-9 and -, starting with a letter (its VMs are k8s-<name>-cp1, -w1, ...)")
    try:
        workers, lb = int(b.get("workers", 1)), int(b.get("lb_ips", 1))
    except (TypeError, ValueError):
        raise Fail(422, "workers, lb_ips: whole numbers")
    if not 0 <= workers <= Conf.max_workers:
        raise Fail(422, f"workers: 0-{Conf.max_workers}")
    if not 0 <= lb <= 8:
        raise Fail(422, "lb_ips: 0-8 extra addresses for LoadBalancer Services")
    api = b.get("api", "tunnel")
    if api not in ("tunnel", "floating"):
        raise Fail(422, "api: tunnel (kubectl through the jump host) or floating (the API server on a floating IP)")
    flavor = b.get("flavor") or Conf.node_flavor
    if not isinstance(flavor, str) or not re.match(r"^[a-z][a-z0-9-]{0,15}$", flavor):
        raise Fail(422, "flavor: a machine type name")
    return dict(name=name, workers=workers, lb_ips=lb, api=api, flavor=flavor, ingress=bool(b.get("ingress", False)))


def job_create(log, kit, me, spec):
    c = q("SELECT * FROM clusters WHERE name=?", (spec["name"],))[0]
    try:
        ensure_tenant_key(kit, log)
        lb = []
        for i in range(spec["lb_ips"]):
            lb.append(kit.call("POST", "/tenant/ips", {"note": f"k8s-{spec['name']}-lb{i + 1}"})["ip"])
            log(f"LoadBalancer address {lb[-1]} (POST /tenant/ips)")
            x("UPDATE clusters SET lb_ips=? WHERE name=?", (json.dumps(lb), c["name"]))
        api_fip = None
        if spec["api"] == "floating":
            api_fip = kit.call("POST", "/tenant/floating-ips", {})["ip"]
            log(f"floating IP {api_fip} for the API server (POST /tenant/floating-ips)")
            x("UPDATE clusters SET api_fip=? WHERE name=?", (api_fip, c["name"]))
        specs = [(node_name(c["name"], "server", 1), "server")] + \
                [(node_name(c["name"], "agent", i), "agent") for i in range(1, spec["workers"] + 1)]
        nodes = create_vms(kit, c, specs, log)
        server, workers = nodes[0], nodes[1:]
        if api_fip:
            kit.call("POST", f"/tenant/floating-ips/{api_fip}/associate", {"target": server["name"]})
            log(f"floating IP {api_fip} -> {server['name']}")
        out = setup_node(server, dict(ROLE="server", NODE_IP=server["ip"], TLS_SANS=api_fip or "",
                                      INGRESS="yes" if spec["ingress"] else "no",
                                      K3S_CHANNEL=Conf.k3s_channel, K3S_VERSION=Conf.k3s_version), log)
        x("UPDATE clusters SET version=? WHERE name=?", (out.split()[-1] if out.startswith("ok k3s") else "", c["name"]))
        if workers:
            join_workers(c, server, workers, log)
        if lb:
            out = ssh(server, ["metallb", Conf.metallb, *lb], timeout=900, log=log)
            log(f"{server['name']}: {out.splitlines()[-1] if out else ''}")
        x("UPDATE clusters SET status='ready', error=NULL WHERE name=?", (c["name"],))
        log(f"cluster {c['name']} is ready: GET {PREFIX}/clusters/{c['name']}/kubeconfig")
    except Fail as e:
        x("UPDATE clusters SET status='error', error=? WHERE name=?", (e.detail, c["name"]))
        raise


def job_scale(log, kit, me, c, workers):
    try:
        server = server_of(c["name"])
        have = nodes_of(c["name"], "agent")
        if workers > len(have):
            used = {int(n["name"].rsplit("w", 1)[1]) for n in have}
            nums = [i for i in range(1, 100) if i not in used][:workers - len(have)]
            new = create_vms(kit, c, [(node_name(c["name"], "agent", i), "agent") for i in nums], log)
            join_workers(c, server, new, log)
        for n in reversed(have[workers:]):
            log(f"removing worker {n['name']}")
            ssh(server, ["remove", n["name"]], timeout=300, log=log, check=False)
            r = kit.call("DELETE", f"/tenant/vms/{n['name']}", ok404=True)
            if r and r.get("job"):
                kit.wait_job(r["job"], Conf.timeout_vm)
            forget_host_key(n["ip"])
            x("DELETE FROM nodes WHERE name=?", (n["name"],))
        x("UPDATE clusters SET workers=?, status='ready', error=NULL WHERE name=?", (workers, c["name"]))
    except Fail as e:
        x("UPDATE clusters SET status='error', error=? WHERE name=?", (e.detail, c["name"]))
        raise


def job_delete(log, kit, me, c):
    side = kit.side
    for e in q("SELECT * FROM exposures WHERE cluster=?", (c["name"],)):
        drop_exposure(kit, e, log)
    for ip in json.loads(c["lb_ips"] or "[]"):
        log(f"releasing LoadBalancer address {ip}")
        kit.call("DELETE", f"{side}/ips/{ip}", ok404=True)
    if c["api_fip"]:
        log(f"releasing floating IP {c['api_fip']}")
        kit.call("DELETE", f"{side}/floating-ips/{c['api_fip']}", ok404=True)
    jobs = []
    for n in nodes_of(c["name"]):
        log(f"deleting VM {n['name']} (DELETE {side}/vms/{n['name']})")
        r = kit.call("DELETE", f"{side}/vms/{n['name']}", ok404=True)
        if r and r.get("job") and side == "/tenant":
            jobs.append(r["job"])
        forget_host_key(n["ip"])
    for j in jobs:
        kit.wait_job(j, Conf.timeout_vm)
    x("DELETE FROM nodes WHERE cluster=?", (c["name"],))
    x("DELETE FROM clusters WHERE name=?", (c["name"],))
    log(f"cluster {c['name']} deleted")


# ------------------------------------------------------------------------------- exposure
def drop_exposure(kit, e, log):
    if e["campus_port"]:
        log(f"closing campus port {e['campus_port']} ({e['namespace']}/{e['service']})")
        kit.call("DELETE", f"{kit.side}/ports/{e['campus_port']}", ok404=True)
    if e["floating_ip"]:
        log(f"releasing floating IP {e['floating_ip']} ({e['namespace']}/{e['service']})")
        kit.call("DELETE", f"{kit.side}/floating-ips/{e['floating_ip']}", ok404=True)
    x("DELETE FROM exposures WHERE cluster=? AND namespace=? AND service=? AND mode=? AND port=? AND protocol=?",
      (e["cluster"], e["namespace"], e["service"], e["mode"], e["port"], e["protocol"]))


def wanted(services):
    """(namespace, service, mode, port, protocol) -> LoadBalancer address, from the Services' annotations"""
    out = {}
    for s in services.get("items", []):
        mode = (s["metadata"].get("annotations") or {}).get(ANNOTATION)
        if s["spec"].get("type") != "LoadBalancer" or mode not in ("port", "floating"):
            continue
        ing = (s.get("status", {}).get("loadBalancer", {}).get("ingress") or [{}])[0].get("ip")
        if not ing:
            continue
        ns, name = s["metadata"]["namespace"], s["metadata"]["name"]
        if mode == "floating":
            out[(ns, name, "floating", 0, "")] = ing
        else:
            for p in s["spec"].get("ports", []):
                out[(ns, name, "port", p["port"], p.get("protocol", "TCP").lower())] = ing
    return out


def sync(kit, c, log=lambda s: None):
    """make the campus ports and floating IPs match the annotated LoadBalancer Services"""
    server = server_of(c["name"])
    want = wanted(json.loads(ssh(server, ["services"], timeout=60)))
    have = {(e["namespace"], e["service"], e["mode"], e["port"], e["protocol"]): e
            for e in q("SELECT * FROM exposures WHERE cluster=?", (c["name"],))}
    for k, e in have.items():
        if want.get(k) != e["lb_ip"] or (e["error"] and kit.side == "/tenant"):
            drop_exposure(kit, e, log)               # gone, moved, or failed before: (re)made below
    have = {(e["namespace"], e["service"], e["mode"], e["port"], e["protocol"]) for e in
            q("SELECT * FROM exposures WHERE cluster=?", (c["name"],))}
    if kit.side == "/tenant":                        # only a tenant user can open ports for the tenant
        for k, ip in want.items():
            if k in have:
                continue
            ns, svc, mode, port, proto = k
            row = dict(cluster=c["name"], namespace=ns, service=svc, mode=mode, lb_ip=ip, port=port, protocol=proto,
                       campus_port=None, connect=None, floating_ip=None, error=None)
            try:
                if mode == "port":
                    r = kit.call("POST", "/tenant/ports", {"target": ip, "port": port, "protocol": proto})
                    row.update(campus_port=r["campus_port"], connect=r["connect"])
                    log(f"{ns}/{svc}: campus port {r['connect']} -> {ip}:{port}/{proto}")
                else:
                    r = kit.call("POST", "/tenant/floating-ips", {"target": ip})
                    row.update(floating_ip=r["ip"], connect=r["ip"])
                    log(f"{ns}/{svc}: floating IP {r['ip']} -> {ip}")
            except Fail as e:
                row["error"] = e.detail
                log(f"{ns}/{svc}: {e.detail}")
            x("INSERT OR REPLACE INTO exposures VALUES (:cluster,:namespace,:service,:mode,:lb_ip,:port,:protocol,"
              ":campus_port,:connect,:floating_ip,:error)", row)
    return exposures_of(c["name"])


def exposures_of(cluster):
    return [{k: v for k, v in e.items() if k != "cluster"} for e in
            q("SELECT * FROM exposures WHERE cluster=? ORDER BY namespace, service, port", (cluster,))]


# ------------------------------------------------------------------------------- views
def cluster_out(c, live=False):
    o = dict(c, lb_ips=json.loads(c["lb_ips"] or "[]"), ingress=bool(c["ingress"]),
             nodes=[{k: n[k] for k in ("name", "role", "ip", "status", "error")} for n in nodes_of(c["name"])],
             exposures=exposures_of(c["name"]))
    if live:
        try:
            out = ssh(server_of(c["name"]), ["nodes"], timeout=30)
            k8s = {f[0]: f for f in (l.split("\t") for l in out.splitlines()) if len(f) >= 4}
            for n in o["nodes"]:
                f = k8s.get(n["name"])
                n["k8s"] = dict(status=f[1], role=f[2], version=f[3]) if f else None
        except Fail as e:
            o["live_error"] = e.detail
    return o


def scope(me, query):
    if me["role"] == "consumer":
        if not me.get("tenant"):
            raise Fail(403, "no tenant")
        return me["tenant"]
    return query.get("tenant") or None


def find_cluster(me, name, query):
    t = scope(me, query)
    rows = q("SELECT * FROM clusters WHERE name=?" + (" AND tenant=?" if t else ""), (name, t) if t else (name,))
    if not rows:
        raise Fail(404, f"no cluster {name}")
    return rows[0]


def kubeconfig(kit, me, c, access):
    server = server_of(c["name"])
    cfg = ssh(server, ["kubeconfig"], timeout=30)
    cfg = cfg.replace("name: default", f"name: {c['name']}").replace("cluster: default", f"cluster: {c['name']}") \
             .replace("user: default", f"user: {c['name']}-admin").replace("current-context: default", f"current-context: {c['name']}")
    if access == "tunnel":
        a = kit.call("GET", "/tenant/access")
        how = (f"keep this running, then use kubectl:  ssh -N -L 6443:{server['ip']}:6443 "
               f"-p {a['jump_port']} {a['jump_user']}@{a['jump_host']}")
        url = "https://127.0.0.1:6443"
    elif access == "internal":
        url, how = f"https://{server['ip']}:6443", "from a VM of the tenant (or staff on the main PC)"
    elif access == "floating":
        if not c["api_fip"]:
            raise Fail(409, f"cluster {c['name']} was created without a floating IP for its API (api: floating)")
        url, how = f"https://{c['api_fip']}:6443", "from campus, directly"
    else:
        raise Fail(422, "access: tunnel, internal or floating")
    cfg = re.sub(r"server: https://[^\s]+", f"server: {url}", cfg)
    return {"cluster": c["name"], "access": access, "server": url, "how": how, "kubeconfig": cfg}


# ------------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "vdc-k8s"

    def log_message(self, fmt, *args):
        if not getattr(self.server, "quiet", False):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def reply(self, code, obj):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            b = json.loads(self.rfile.read(n))
        except ValueError:
            raise Fail(422, "the body is not JSON")
        if not isinstance(b, dict):
            raise Fail(422, "the body must be a JSON object")
        return b

    def route(self):
        try:
            path, _, qs = self.path.partition("?")
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(qs).items()}
            if path.rstrip("/") == "/health":
                return self.reply(200, {"ok": True})
            if not (path == PREFIX or path.startswith(PREFIX + "/")):
                raise Fail(404, "not found")
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                raise Fail(401, "log in to the cloud API first (Authorization: Bearer <token>)")
            me = whoami(auth[7:])
            parts = [p for p in path[len(PREFIX):].split("/") if p]
            code, obj = self.dispatch(me, Kit(auth[7:], me["role"]), self.command, parts, query)
            return self.reply(code, obj)
        except Fail as e:
            return self.reply(e.code, {"detail": e.detail})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = route

    def dispatch(self, me, kit, m, p, query):
        role = me["role"]
        if m != "GET" and role not in ("consumer", "provider"):
            raise Fail(403, "read only (auditor)")
        if p == ["config"] and m == "GET":
            return 200, dict(node_flavor=Conf.node_flavor, node_image=Conf.node_image, max_clusters=Conf.max_clusters,
                             max_workers=Conf.max_workers, k3s=Conf.k3s_version or f"{Conf.k3s_channel} channel",
                             metallb=Conf.metallb, annotation=ANNOTATION)
        if p[:1] == ["jobs"] and m == "GET":
            t = scope(me, query)
            if len(p) == 1:
                rows = q("SELECT * FROM jobs" + (" WHERE tenant=?" if t else "") + " ORDER BY created DESC LIMIT 100",
                         (t,) if t else ())
                return 200, [job_out(j) for j in rows]
            rows = q("SELECT * FROM jobs WHERE id=?" + (" AND tenant=?" if t else ""), (p[1], t) if t else (p[1],))
            if not rows:
                raise Fail(404, f"no job {p[1]}")
            return 200, job_out(rows[0])
        if p[:1] != ["clusters"]:
            raise Fail(404, "not found")
        t = scope(me, query)
        if len(p) == 1 and m == "GET":
            rows = q("SELECT * FROM clusters" + (" WHERE tenant=?" if t else "") + " ORDER BY tenant, name", (t,) if t else ())
            return 200, [cluster_out(c) for c in rows]
        if len(p) == 1 and m == "POST":
            if role != "consumer":
                raise Fail(403, "clusters are created by a tenant user")
            spec = check_cluster(self.body())
            if q("SELECT 1 FROM clusters WHERE name=?", (spec["name"],)):
                raise Fail(409, f"cluster {spec['name']} exists (names are unique in the whole cloud, like VM names)")
            n = len(q("SELECT 1 FROM clusters WHERE tenant=?", (me["tenant"],)))
            if n >= Conf.max_clusters:
                raise Fail(409, f"tenant {me['tenant']} has {n} cluster(s), the most allowed ({Conf.max_clusters})")
            x("INSERT INTO clusters VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (spec["name"], me["tenant"], spec["workers"], spec["flavor"], Conf.node_image, "[]", spec["api"], None,
               int(spec["ingress"]), Conf.k3s_version, "creating", None, now()))
            return 202, run_job(new_job(me, "cluster-create", spec["name"]), job_create, kit, me, spec)
        c = find_cluster(me, p[1], query)
        busy = c["status"] in ("creating", "scaling", "deleting")
        if len(p) == 2 and m == "GET":
            return 200, cluster_out(c, live=query.get("state") == "true")
        if len(p) == 2 and m == "DELETE":
            if busy and c["status"] != "creating":
                raise Fail(409, f"cluster {c['name']} is {c['status']}")
            if c["status"] == "creating" and q("SELECT 1 FROM jobs WHERE target=? AND kind='cluster-create' AND state IN ('queued','started')", (c["name"],)):
                raise Fail(409, f"cluster {c['name']} is still being created: wait for its job")
            x("UPDATE clusters SET status='deleting' WHERE name=?", (c["name"],))
            return 202, run_job(new_job(me, "cluster-delete", c["name"], c["tenant"]), job_delete, kit, me, c)
        if len(p) == 2 and m == "PATCH":
            if role != "consumer":
                raise Fail(403, "a tenant user scales the cluster")
            try:
                workers = int(self.body().get("workers"))
            except (TypeError, ValueError):
                raise Fail(422, "workers: a whole number")
            if not 0 <= workers <= Conf.max_workers:
                raise Fail(422, f"workers: 0-{Conf.max_workers}")
            if c["status"] != "ready":
                raise Fail(409, f"cluster {c['name']} is {c['status']}")
            x("UPDATE clusters SET status='scaling' WHERE name=?", (c["name"],))
            return 202, run_job(new_job(me, "cluster-scale", c["name"]), job_scale, kit, me, c, workers)
        if c["status"] in ("creating", "deleting"):        # the rest needs a working control plane (server_of)
            raise Fail(409, f"cluster {c['name']} is {c['status']}")
        if p[2:] == ["kubeconfig"] and m == "GET":
            if role != "consumer":
                raise Fail(403, "the kubeconfig is for the tenant's own users")
            return 200, kubeconfig(kit, me, c, query.get("access", "tunnel"))
        if p[2:] == ["sync"] and m == "POST":
            lines = []
            return 200, {"exposures": sync(kit, c, lines.append), "log": lines}
        if p[2:] == ["kubectl"] and m == "POST":
            if role != "consumer":
                raise Fail(403, "kubectl is for the tenant's own users")
            b = self.body()
            args, stdin = b.get("args"), b.get("stdin")
            if isinstance(args, str):
                args = shlex.split(args)
            if not args or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise Fail(422, "args: kubectl's arguments, e.g. [\"get\", \"pods\", \"-A\"]")
            if stdin is not None and (not isinstance(stdin, str) or len(stdin) > 262144):
                raise Fail(422, "stdin: text (a manifest for -f -), at most 256 KB")
            code, out = ssh(server_of(c["name"]), ["kubectl", *args], stdin=stdin, timeout=150, check=False)
            lines = []
            exposures = sync(kit, c, lines.append)
            return 200, {"exit_code": code, "output": out[-131072:], "exposures": exposures, "sync": lines}
        if p[2:] == ["services"] and m == "GET":
            svcs = json.loads(ssh(server_of(c["name"]), ["services"], timeout=60))
            return 200, [dict(namespace=s["metadata"]["namespace"], name=s["metadata"]["name"], type=s["spec"].get("type"),
                              cluster_ip=s["spec"].get("clusterIP"),
                              lb_ip=((s.get("status", {}).get("loadBalancer", {}).get("ingress") or [{}])[0].get("ip")),
                              ports=[dict(port=pp["port"], protocol=pp.get("protocol"), node_port=pp.get("nodePort"))
                                     for pp in s["spec"].get("ports", [])],
                              expose=(s["metadata"].get("annotations") or {}).get(ANNOTATION))
                         for s in svcs.get("items", [])]
        raise Fail(404, "not found")


# ------------------------------------------------------------------------------- main
def main(argv=None):
    env = os.environ.get
    ap = argparse.ArgumentParser(description="Kubernetes service add-on for the vdc-cluster cloud API")
    ap.add_argument("--api", default=env("VDC_API", Conf.api), help="the cloud API as this host reaches it")
    ap.add_argument("--bind", default=env("VDC_K8S_BIND", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(env("VDC_K8S_PORT", "8092")))
    ap.add_argument("--state", default=env("VDC_K8S_STATE", Conf.state), help="database, SSH key, known_hosts")
    ap.add_argument("--node-flavor", default=env("VDC_K8S_FLAVOR", Conf.node_flavor), help="default machine type of a node")
    ap.add_argument("--node-image", default=env("VDC_K8S_IMAGE", Conf.node_image), help="image of the nodes")
    ap.add_argument("--max-clusters", type=int, default=int(env("VDC_K8S_MAX_CLUSTERS", str(Conf.max_clusters))))
    ap.add_argument("--max-workers", type=int, default=int(env("VDC_K8S_MAX_WORKERS", str(Conf.max_workers))))
    ap.add_argument("--k3s-channel", default=env("VDC_K8S_CHANNEL", Conf.k3s_channel), help="k3s release channel (stable, latest, v1.33, ...)")
    ap.add_argument("--k3s-version", default=env("VDC_K8S_VERSION", Conf.k3s_version), help="a fixed k3s version, e.g. v1.33.5+k3s1")
    ap.add_argument("--metallb", default=env("VDC_K8S_METALLB", Conf.metallb), help="MetalLB version")
    ap.add_argument("--check", action="store_true", help="check the API and the SSH key, then exit")
    ap.add_argument("--quiet", action="store_true", help="no request log")
    a = ap.parse_args(argv)
    Conf.api, Conf.state = a.api.rstrip("/"), os.path.abspath(a.state)
    Conf.node_flavor, Conf.node_image = a.node_flavor, a.node_image
    Conf.max_clusters, Conf.max_workers = a.max_clusters, a.max_workers
    Conf.k3s_channel, Conf.k3s_version, Conf.metallb = a.k3s_channel, a.k3s_version, a.metallb
    Conf.ssh = shlex.split(env("VDC_K8S_SSH", "ssh"))
    pub = ensure_key()
    db()
    if a.check:
        try:
            with urllib.request.urlopen(Conf.api + "/docs", timeout=10):
                pass
            print(f"ok: cloud API {Conf.api} answers; SSH key {key_path()} ({pub.split()[0]}); database {Conf.state}/k8s.db")
            return 0
        except (urllib.error.URLError, OSError) as e:
            print(f"vdc-k8s: the cloud API at {Conf.api} does not answer ({getattr(e, 'reason', e)})", file=sys.stderr)
            return 1
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    srv.quiet = a.quiet
    print(f"vdc-k8s on http://{a.bind}:{a.port}{PREFIX}  ->  API {Conf.api}; nodes: {Conf.node_flavor} {Conf.node_image}, "
          f"k3s {Conf.k3s_version or Conf.k3s_channel}, MetalLB {Conf.metallb}; state {Conf.state}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
