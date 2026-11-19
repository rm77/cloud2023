#!/usr/bin/env python3
"""vdc-containers: a container service for vdc-cluster, as an add-on that uses the cloud API.

Tenants run Docker containers and Compose stacks without managing a VM themselves. The add-on
puts them on container hosts: ordinary VMs of the tenant (ctr-<tenant>-N), which it creates
through the API with the caller's own token, so the tenant's quota, ownership and VM-hour
billing apply as for any VM. It then sets the host up over SSH (Docker, the vdc-host helper)
and runs `vdc-host` there for every action. The kit is not changed.

    python3 vdc_containers.py --api http://127.0.0.1:36100        # on the main PC
    python3 vdc_containers.py --check                              # API reachable? SSH key?

Every request carries the caller's API token (Authorization: Bearer ...), as with the API;
portal.py passes /addon/containers on to this service (--addon containers=http://...:8091).
Python 3.8+ standard library only. It must run where the tenant subnets are routed (the main
PC), as any user with an `ssh` client.
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
PREFIX = "/addon/containers"

NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
IMAGE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,199}$")
ENVKEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
RESERVED = {"hosts", "stacks", "jobs", "config"}
EXPOSE = ("internal", "ports", "floating")


class Conf:
    api = "http://127.0.0.1:36100"
    state = os.path.join(HERE, "state")
    host_flavor = "medium"
    host_image = "alpine"
    max_hosts = 2
    reserve_mb = 256
    host_ports = (20000, 20999)
    ssh = ["ssh"]
    timeout_vm = 900             # s: VM creation job
    timeout_ssh = 600            # s: until a new host answers SSH


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Fail(Exception):
    """an answer to the caller: HTTP code and message"""
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code, self.detail = code, detail


# ------------------------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (name TEXT PRIMARY KEY, tenant TEXT, ip TEXT, user TEXT, root TEXT,
    flavor TEXT, vcpus INTEGER, mem_mb INTEGER, status TEXT, error TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS containers (tenant TEXT, name TEXT, host TEXT, image TEXT, command TEXT,
    cpus REAL, mem_mb INTEGER, env TEXT, ports TEXT, expose TEXT, address TEXT, floating_ip TEXT,
    status TEXT, error TEXT, created TEXT, PRIMARY KEY (tenant, name));
CREATE TABLE IF NOT EXISTS stacks (tenant TEXT, name TEXT, host TEXT, compose TEXT, mem_mb INTEGER,
    ports TEXT, expose TEXT, status TEXT, error TEXT, created TEXT, PRIMARY KEY (tenant, name));
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, tenant TEXT, username TEXT, kind TEXT, target TEXT,
    state TEXT, log TEXT, error TEXT, created TEXT, ended TEXT);
"""
_db_lock = threading.RLock()
_db = None


def db():
    global _db
    if _db is None:
        os.makedirs(Conf.state, exist_ok=True)
        _db = sqlite3.connect(os.path.join(Conf.state, "containers.db"), check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.executescript(SCHEMA)
        # a job that was running when the service stopped will not finish
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
    """the cloud API, with one caller's token"""
    def __init__(self, token):
        self.token = token

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

    def wait_job(self, job_id, log, limit):
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


# ------------------------------------------------------------------------------- SSH to the hosts
def key_path():
    return os.path.join(Conf.state, "id_containers")


def ensure_key():
    k = key_path()
    if not os.path.exists(k):
        os.makedirs(Conf.state, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "vdc-containers", "-f", k], check=True)
    with open(k + ".pub") as f:
        return f.read().strip()


def ssh(host, args, stdin=None, timeout=300, log=None, check=True):
    """run `vdc-host ARGS` (or a raw command when args is a str) on a host, as root"""
    if isinstance(args, str):
        remote = args
    else:
        remote = " ".join(shlex.quote(str(a)) for a in ["/usr/local/bin/vdc-host", *args])
        if host.get("root"):
            remote = host["root"] + " " + remote
    argv = Conf.ssh + ["-i", key_path(), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                       "-o", "StrictHostKeyChecking=accept-new",
                       "-o", "UserKnownHostsFile=" + os.path.join(Conf.state, "known_hosts"),
                       f"{host['user']}@{host['ip']}", remote]
    if log:
        log(f"{host['name']}$ {remote}")
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Fail(504, f"{host['name']}: no answer in {timeout} s")
    out = (p.stdout + p.stderr).strip()
    if check and p.returncode != 0:
        raise Fail(409 if p.returncode != 255 else 502,
                   f"{host['name']}: " + (out.splitlines()[-1] if out else f"ssh exit {p.returncode}"))
    return out if check else (p.returncode, out)


def forget_host_key(ip):
    kh = os.path.join(Conf.state, "known_hosts")
    if os.path.exists(kh):
        subprocess.run(["ssh-keygen", "-R", ip, "-f", kh], capture_output=True)


def setup_payload():
    with open(os.path.join(HERE, "host", "vdc-host")) as f:
        helper = f.read()
    with open(os.path.join(HERE, "host", "host-setup.sh")) as f:
        setup = f.read()
    return ("set -e\nmkdir -p /usr/local/bin\ncat >/usr/local/bin/vdc-host <<'VDC_HOST_EOF'\n" + helper +
            "VDC_HOST_EOF\nchmod 755 /usr/local/bin/vdc-host\n" + setup)


# ------------------------------------------------------------------------------- jobs
def new_job(me, kind, target):
    jid = f"{kind}-{uuid.uuid4().hex[:12]}"
    x("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
      (jid, me.get("tenant"), me["username"], kind, target, "queued", "", None, now(), None))
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
        except Exception as e:                       # noqa: BLE001  (the job records any failure)
            msg = e.detail if isinstance(e, Fail) else f"{e.__class__.__name__}: {e}"
            logger("failed: " + msg)
            x("UPDATE jobs SET state='failed', error=?, ended=? WHERE id=?", (msg, now(), jid))
    t = threading.Thread(target=body, name=jid, daemon=True)
    t.start()
    return t


def job_out(j):
    return dict(j, log=j["log"].splitlines() if j.get("log") else [], status_url=f"{PREFIX}/jobs/{j['id']}")


# ------------------------------------------------------------------------------- hosts
_tenant_locks = {}


def tenant_lock(t):
    with _db_lock:
        return _tenant_locks.setdefault(t, threading.Lock())


def host_name(tenant, n):
    return f"{('ctr-' + tenant)[:12].rstrip('-')}-{n}"


def used_mb(host):
    a = q("SELECT COALESCE(SUM(mem_mb),0) m FROM containers WHERE host=? AND status!='error'", (host,))[0]["m"]
    b = q("SELECT COALESCE(SUM(mem_mb),0) m FROM stacks WHERE host=? AND status!='error'", (host,))[0]["m"]
    return a + b


def free_mb(h):
    return h["mem_mb"] - Conf.reserve_mb - used_mb(h["name"])


def pick_host(tenant, mem_mb):
    """the fullest ready host that still fits: fewer hosts, fewer VM hours"""
    fits = [h for h in q("SELECT * FROM hosts WHERE tenant=? AND status='ready' ORDER BY name", (tenant,))
            if free_mb(h) >= mem_mb]
    return min(fits, key=free_mb) if fits else None


def ensure_tenant_key(kit, log):
    pub = ensure_key()
    if not any(k.get("comment") == "vdc-containers" for k in kit.call("GET", "/tenant/keys")):
        log("adding the add-on's SSH key to the tenant (POST /tenant/keys): new VMs accept it")
        kit.call("POST", "/tenant/keys", {"key": pub})


def create_host(kit, me, log):
    tenant = me["tenant"]
    hosts = q("SELECT * FROM hosts WHERE tenant=?", (tenant,))
    if len(hosts) >= Conf.max_hosts:
        raise Fail(409, f"tenant {tenant} has {len(hosts)} container hosts, the most allowed ({Conf.max_hosts}); "
                        "delete containers or an empty host")
    flavors = {f["name"]: f for f in kit.call("GET", "/tenant/flavors")}
    f = flavors.get(Conf.host_flavor)
    if not f:
        raise Fail(409, f"machine type {Conf.host_flavor} (--host-flavor) does not exist")
    ensure_tenant_key(kit, log)
    taken = {h["name"] for h in q("SELECT name FROM hosts")}
    for n in range(1, 100):
        name = host_name(tenant, n)
        if name in taken:
            continue
        log(f"new container host {name}: POST /tenant/vms flavor {Conf.host_flavor}, image {Conf.host_image}")
        try:
            acc = kit.call("POST", "/tenant/vms", {"name": name, "flavor": Conf.host_flavor, "image": Conf.host_image})
            break
        except Fail as e:
            if e.code == 409 and "exists" in e.detail:      # the name is used by another VM
                continue
            raise
    else:
        raise Fail(409, "no free container host name")
    h = dict(name=name, tenant=tenant, ip=None, user=None, root=None, flavor=f["name"], vcpus=f["vcpus"],
             mem_mb=f["mem_mb"], status="creating", error=None, created=now())
    x("INSERT INTO hosts VALUES (:name,:tenant,:ip,:user,:root,:flavor,:vcpus,:mem_mb,:status,:error,:created)", h)
    try:
        log(f"waiting for job {acc['job']}")
        kit.wait_job(acc["job"], log, Conf.timeout_vm)
        vm = kit.call("GET", f"/tenant/vms/{name}")
        user = next((g["user"] for g in kit.call("GET", "/tenant/access")["guests"] if g["name"] == name), None)
        h.update(ip=vm["ip"], user=user or "alpine", status="setting-up")
        x("UPDATE hosts SET ip=?, user=?, status=? WHERE name=?", (h["ip"], h["user"], h["status"], name))
        forget_host_key(h["ip"])
        setup_host(h, log)
        return h
    except Fail as e:
        x("UPDATE hosts SET status='error', error=? WHERE name=?", (e.detail, name))
        raise


def setup_host(h, log):
    log(f"{h['name']} ({h['ip']}): waiting for SSH as {h['user']}")
    end = time.time() + Conf.timeout_ssh
    while True:
        code, out = ssh(h, "id -u; command -v doas || true; command -v sudo || true", timeout=30, check=False)
        if code == 0:
            break
        if time.time() > end:
            raise Fail(504, f"{h['name']}: no SSH in {Conf.timeout_ssh} s ({out.splitlines()[-1] if out else ''})")
        time.sleep(10)
    lines = out.split()
    root = ""
    if lines and lines[0] != "0":
        for cand in ("doas -n", "sudo -n"):
            if any(l.endswith("/" + cand.split()[0]) for l in lines) and ssh(h, cand + " true", timeout=30, check=False)[0] == 0:
                root = cand
                break
        else:
            raise Fail(409, f"{h['name']}: {h['user']} can't become root without a password (doas or sudo)")
    h["root"] = root
    x("UPDATE hosts SET root=? WHERE name=?", (root, h["name"]))
    log(f"{h['name']}: installing vdc-host and Docker (host-setup.sh)")
    out = ssh(h, (root + " sh -s").strip(), stdin=setup_payload(), timeout=900, log=None)
    for line in out.splitlines()[-3:]:
        log(f"{h['name']}: {line}")
    x("UPDATE hosts SET status='ready', error=NULL WHERE name=?", (h["name"],))
    h["status"] = "ready"


def host_or_new(kit, me, mem_mb, log):
    h = pick_host(me["tenant"], mem_mb)
    if h:
        log(f"placed on {h['name']} ({free_mb(h)} MB free)")
        return h
    n = len(q("SELECT name FROM hosts WHERE tenant=? AND status!='deleting'", (me["tenant"],)))
    if n >= Conf.max_hosts:
        raise Fail(409, f"no container host has {mem_mb} MB free, and the tenant has the most hosts allowed "
                        f"({Conf.max_hosts}): delete a container, or an empty or broken host")
    f = next((f for f in kit.call("GET", "/tenant/flavors") if f["name"] == Conf.host_flavor), None)
    if f and f["mem_mb"] - Conf.reserve_mb < mem_mb:
        raise Fail(422, f"{mem_mb} MB is more than a container host has ({f['mem_mb']} MB, {Conf.reserve_mb} MB kept for the host)")
    return create_host(kit, me, log)


# ------------------------------------------------------------------------------- containers
def check_ports(ports):
    out = []
    for p in ports or []:
        if not isinstance(p, dict):
            p = {"port": p}
        port, proto = p.get("port"), p.get("protocol", "tcp")
        if not isinstance(port, int) or not 1 <= port <= 65535 or proto not in ("tcp", "udp"):
            raise Fail(422, f"ports: {p!r}: port 1-65535, protocol tcp or udp")
        if any(o["port"] == port and o["protocol"] == proto for o in out):
            raise Fail(422, f"ports: {port}/{proto} twice")
        out.append({"port": port, "protocol": proto})
    return out


def check_container(b):
    name = b.get("name", "")
    if not isinstance(name, str) or not NAME.match(name) or name in RESERVED:
        raise Fail(422, "name: a-z, 0-9 and -, starting with a letter, at most 31 (not hosts/stacks/jobs/config)")
    if not isinstance(b.get("image"), str) or not IMAGE.match(b["image"]):
        raise Fail(422, "image: an image reference such as nginx:alpine or ghcr.io/org/app:1.2")
    cmd = b.get("command") or []
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    if not isinstance(cmd, list) or not all(isinstance(c, str) and "\0" not in c for c in cmd) or len(cmd) > 64:
        raise Fail(422, "command: a list of strings")
    env = b.get("env") or {}
    if not isinstance(env, dict) or not all(ENVKEY.match(k) and isinstance(v, str) and len(v) <= 4096 and "\0" not in v
                                            for k, v in env.items()):
        raise Fail(422, "env: {\"NAME\": \"value\"}")
    try:
        cpus = float(b.get("cpus", 0.5))
        mem = int(b.get("mem_mb", 256))
    except (TypeError, ValueError):
        raise Fail(422, "cpus: a number; mem_mb: whole MB")
    if not 0.1 <= cpus <= 8 or not 32 <= mem <= 16384:
        raise Fail(422, "cpus 0.1-8, mem_mb 32-16384")
    expose = b.get("expose", "internal")
    if expose not in EXPOSE:
        raise Fail(422, "expose: internal (my subnet only), ports (a campus port each) or floating (a floating IP)")
    ports = check_ports(b.get("ports"))
    if expose != "internal" and not ports:
        raise Fail(422, f"expose {expose} needs ports")
    return dict(name=name, image=b["image"], command=cmd, env=env, cpus=cpus, mem_mb=mem, expose=expose, ports=ports)


def free_host_ports(host, n):
    used = set()
    for c in q("SELECT ports FROM containers WHERE host=?", (host,)):
        used |= {p.get("host_port") for p in json.loads(c["ports"] or "[]")}
    for s in q("SELECT ports FROM stacks WHERE host=?", (host,)):
        used |= {p["port"] for p in json.loads(s["ports"] or "[]")}
    lo, hi = Conf.host_ports
    free = [p for p in range(lo, hi + 1) if p not in used][:n]
    if len(free) < n:
        raise Fail(409, f"{host}: no free host ports in {lo}-{hi}")
    return free


def subnet_prefix(kit):
    return kit.call("GET", "/tenant/me")["subnet"].split("/")[1]


def job_create_container(log, kit, me, spec):
    tenant = me["tenant"]
    ports = spec["ports"]
    address = fip = None
    try:
        with tenant_lock(tenant):
            h = host_or_new(kit, me, spec["mem_mb"], log)
            if spec["expose"] != "floating":
                for p, hp in zip(ports, free_host_ports(h["name"], len(ports))):
                    p["host_port"] = hp
            x("UPDATE containers SET host=?, ports=? WHERE tenant=? AND name=?",
              (h["name"], json.dumps(ports), tenant, spec["name"]))
        run = ["run", "--name", spec["name"], "--label", "vdc.managed=1", "--label", f"vdc.tenant={tenant}",
               "--restart", "unless-stopped", "--cpus", spec["cpus"], "--memory", f"{spec['mem_mb']}m"]
        for k, v in spec["env"].items():
            run += ["-e", f"{k}={v}"]
        if spec["expose"] == "floating":
            address = kit.call("POST", "/tenant/ips", {"note": f"ctr-{spec['name']}"[:32]})["ip"]
            log(f"own address {address} (POST /tenant/ips), on {h['name']}")
            x("UPDATE containers SET address=? WHERE tenant=? AND name=?", (address, tenant, spec["name"]))
            ssh(h, ["addr", "add", f"{address}/{subnet_prefix(kit)}"], log=log)
            for p in ports:
                run += ["-p", f"{address}:{p['port']}:{p['port']}/{p['protocol']}"]
        else:
            address = h["ip"]
            for p in ports:
                run += ["-p", f"{p['host_port']}:{p['port']}/{p['protocol']}"]
        run += [spec["image"], *spec["command"]]
        ssh(h, run, timeout=900, log=log)                       # pulls the image the first time
        if spec["expose"] == "ports":
            for p in ports:
                r = kit.call("POST", "/tenant/ports", {"target": h["name"], "port": p["host_port"], "protocol": p["protocol"]})
                p["campus_port"], p["connect"] = r["campus_port"], r["connect"]
                log(f"campus port {r['connect']} -> {h['name']}:{p['host_port']} -> container :{p['port']}")
        if spec["expose"] == "floating":
            fip = kit.call("POST", "/tenant/floating-ips", {"target": address})["ip"]
            log(f"floating IP {fip} -> {address}")
        x("UPDATE containers SET ports=?, address=?, floating_ip=?, status='running', error=NULL WHERE tenant=? AND name=?",
          (json.dumps(ports), address, fip, tenant, spec["name"]))
    except Fail as e:
        x("UPDATE containers SET status='error', error=?, ports=?, address=?, floating_ip=? WHERE tenant=? AND name=?",
          (e.detail, json.dumps(ports), address, fip, tenant, spec["name"]))
        raise


def release_network(kit, me, c, h, log):
    """the campus ports, floating IP and own address of a container (gone ones are fine)"""
    side = "/provider" if me["role"] == "provider" else "/tenant"
    for p in json.loads(c["ports"] or "[]"):
        if p.get("campus_port"):
            log(f"closing campus port {p['campus_port']}")
            kit.call("DELETE", f"{side}/ports/{p['campus_port']}", ok404=True)
    if c.get("floating_ip"):
        log(f"releasing floating IP {c['floating_ip']}")
        kit.call("DELETE", f"{side}/floating-ips/{c['floating_ip']}", ok404=True)
    if c.get("expose") == "floating" and c.get("address"):
        if h and h.get("status") == "ready":
            ssh(h, ["addr", "del", c["address"]], log=log, check=False)
        log(f"releasing own address {c['address']}")
        kit.call("DELETE", f"{side}/ips/{c['address']}", ok404=True)


def job_delete_container(log, kit, me, c):
    h = one_host(c["host"]) if c.get("host") else None
    if h and h["status"] == "ready":
        code, out = ssh(h, ["rm", c["name"]], log=log, check=False)
        if code != 0:
            log(f"{h['name']} did not answer ({out}); the container goes with the host")
    try:
        release_network(kit, me, c, h, log)
    except Fail as e:
        x("UPDATE containers SET status='error', error=? WHERE tenant=? AND name=?", (e.detail, c["tenant"], c["name"]))
        raise
    x("DELETE FROM containers WHERE tenant=? AND name=?", (c["tenant"], c["name"]))
    log(f"container {c['name']} deleted")


# ------------------------------------------------------------------------------- stacks
def check_stack(b):
    name = b.get("name", "")
    if not isinstance(name, str) or not NAME.match(name) or name in RESERVED:
        raise Fail(422, "name: a-z, 0-9 and -, starting with a letter, at most 31")
    compose = b.get("compose")
    if not isinstance(compose, str) or not compose.strip() or len(compose) > 65536:
        raise Fail(422, "compose: the compose.yaml text (at most 64 KB)")
    try:
        mem = int(b.get("mem_mb", 512))
    except (TypeError, ValueError):
        raise Fail(422, "mem_mb: whole MB")
    if not 64 <= mem <= 16384:
        raise Fail(422, "mem_mb 64-16384: memory to keep free for the whole stack on its host")
    expose = b.get("expose", "internal")
    if expose not in ("internal", "ports"):
        raise Fail(422, "expose: internal or ports")
    ports = check_ports(b.get("ports"))
    if expose == "ports" and not ports:
        raise Fail(422, "expose ports needs ports: the host ports the compose file publishes")
    return dict(name=name, compose=compose, mem_mb=mem, expose=expose, ports=ports)


def job_create_stack(log, kit, me, spec):
    tenant = me["tenant"]
    ports = spec["ports"]
    try:
        with tenant_lock(tenant):
            h = host_or_new(kit, me, spec["mem_mb"], log)
            used = set()
            for c in q("SELECT ports FROM containers WHERE host=?", (h["name"],)):
                used |= {p.get("host_port") for p in json.loads(c["ports"] or "[]")}
            for s in q("SELECT name, ports FROM stacks WHERE host=? AND NOT (tenant=? AND name=?)", (h["name"], tenant, spec["name"])):
                used |= {p["port"] for p in json.loads(s["ports"] or "[]")}
            clash = [p["port"] for p in spec["ports"] if p["port"] in used]
            if clash:
                raise Fail(409, f"{h['name']}: host port(s) {clash} already used there")
            x("UPDATE stacks SET host=? WHERE tenant=? AND name=?", (h["name"], tenant, spec["name"]))
        out = ssh(h, ["stack", "up", spec["name"]], stdin=spec["compose"], timeout=1200, log=log)
        for line in out.splitlines()[-5:]:
            log(f"{h['name']}: {line}")
        if spec["expose"] == "ports":
            for p in ports:
                r = kit.call("POST", "/tenant/ports", {"target": h["name"], "port": p["port"], "protocol": p["protocol"]})
                p["campus_port"], p["connect"] = r["campus_port"], r["connect"]
                log(f"campus port {r['connect']} -> {h['name']}:{p['port']}")
        x("UPDATE stacks SET ports=?, status='running', error=NULL WHERE tenant=? AND name=?",
          (json.dumps(ports), tenant, spec["name"]))
    except Fail as e:
        x("UPDATE stacks SET status='error', error=?, ports=? WHERE tenant=? AND name=?",
          (e.detail, json.dumps(ports), tenant, spec["name"]))
        raise


def job_delete_stack(log, kit, me, s):
    h = one_host(s["host"]) if s.get("host") else None
    if h and h["status"] == "ready":
        code, out = ssh(h, ["stack", "down", s["name"]], timeout=600, log=log, check=False)
        if code != 0:
            log(f"{h['name']} did not answer ({out}); the stack goes with the host")
    side = "/provider" if me["role"] == "provider" else "/tenant"
    for p in json.loads(s["ports"] or "[]"):
        if p.get("campus_port"):
            kit.call("DELETE", f"{side}/ports/{p['campus_port']}", ok404=True)
    x("DELETE FROM stacks WHERE tenant=? AND name=?", (s["tenant"], s["name"]))
    log(f"stack {s['name']} deleted")


def job_delete_host(log, kit, me, h):
    side = "/provider" if me["role"] == "provider" else "/tenant"
    log(f"deleting VM {h['name']} (DELETE {side}/vms/{h['name']})")
    r = kit.call("DELETE", f"{side}/vms/{h['name']}", ok404=True)
    if r and r.get("job") and side == "/tenant":
        kit.wait_job(r["job"], log, Conf.timeout_vm)
    if h.get("ip"):
        forget_host_key(h["ip"])
    x("DELETE FROM hosts WHERE name=?", (h["name"],))


# ------------------------------------------------------------------------------- views
def one_host(name):
    r = q("SELECT * FROM hosts WHERE name=?", (name,))
    return r[0] if r else None


def host_out(h):
    return dict(name=h["name"], tenant=h["tenant"], ip=h["ip"], flavor=h["flavor"], vcpus=h["vcpus"], mem_mb=h["mem_mb"],
                free_mb=max(0, free_mb(h)), status=h["status"], error=h["error"], created=h["created"],
                containers=[c["name"] for c in q("SELECT name FROM containers WHERE host=?", (h["name"],))],
                stacks=[s["name"] for s in q("SELECT name FROM stacks WHERE host=?", (h["name"],))])


def container_out(c, states=None):
    o = dict(c, command=json.loads(c["command"] or "[]"), env=json.loads(c["env"] or "{}"),
             ports=json.loads(c["ports"] or "[]"))
    if states is not None:
        st = states.get((c["host"], c["name"]))
        o["state"], o["state_detail"] = (st or ("unknown", ""))
    return o


def stack_out(s, states=None):
    o = dict(s, ports=json.loads(s["ports"] or "[]"))
    if states is not None:
        o["services"] = states.get((s["host"], s["name"]), [])
    return o


def live_states(hosts):
    """(host, container) -> (state, status) from `vdc-host ps` on each host, in parallel"""
    out, threads = {}, []

    def one(h):
        code, txt = ssh(h, ["ps"], timeout=30, check=False)
        for line in (txt.splitlines() if code == 0 else []):
            f = line.split("\t")
            if len(f) >= 2:
                out[(h["name"], f[0])] = (f[1], f[2] if len(f) > 2 else "")
    for h in hosts:
        if h and h["status"] == "ready":
            t = threading.Thread(target=one, args=(h,))
            t.start()
            threads.append(t)
    for t in threads:
        t.join(40)
    return out


def scope(me, query):
    """the tenant a request may see: its own for a consumer, any (or ?tenant=) for provider/auditor"""
    if me["role"] == "consumer":
        if not me.get("tenant"):
            raise Fail(403, "no tenant")
        return me["tenant"]
    return query.get("tenant") or None


def find(table, me, name, query):
    t = scope(me, query)
    rows = q(f"SELECT * FROM {table} WHERE name=?" + (" AND tenant=?" if t else ""), (name, t) if t else (name,))
    if not rows:
        raise Fail(404, f"no {table[:-1]} {name}")
    if len(rows) > 1:
        raise Fail(409, f"{name} exists in several tenants: add ?tenant=")
    return rows[0]


# ------------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "vdc-containers"

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
            token = auth[7:]
            me = dict(whoami(token), _token=token)
            parts = [p for p in path[len(PREFIX):].split("/") if p]
            code, obj = self.dispatch(me, Kit(token), self.command, parts, query)
            return self.reply(code, obj)
        except Fail as e:
            return self.reply(e.code, {"detail": e.detail})

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = route

    # --------------------------------------------------------------- the routes
    def dispatch(self, me, kit, m, p, query):
        role = me["role"]
        writer = role in ("consumer", "provider")
        if m != "GET" and not writer:
            raise Fail(403, "read only (auditor)")
        if p == ["config"] and m == "GET":
            return 200, dict(host_flavor=Conf.host_flavor, host_image=Conf.host_image, max_hosts=Conf.max_hosts,
                             reserve_mb=Conf.reserve_mb, host_ports=f"{Conf.host_ports[0]}-{Conf.host_ports[1]}",
                             expose=list(EXPOSE))
        # jobs
        if p[:1] == ["jobs"]:
            t = scope(me, query)
            if len(p) == 1 and m == "GET":
                rows = q("SELECT * FROM jobs" + (" WHERE tenant=?" if t else "") + " ORDER BY created DESC LIMIT 100",
                         (t,) if t else ())
                return 200, [job_out(j) for j in rows]
            if len(p) == 2 and m == "GET":
                rows = q("SELECT * FROM jobs WHERE id=?" + (" AND tenant=?" if t else ""), (p[1], t) if t else (p[1],))
                if not rows:
                    raise Fail(404, f"no job {p[1]}")
                return 200, job_out(rows[0])
        # hosts
        if p[:1] == ["hosts"]:
            t = scope(me, query)
            if len(p) == 1 and m == "GET":
                return 200, [host_out(h) for h in q("SELECT * FROM hosts" + (" WHERE tenant=?" if t else "") + " ORDER BY name",
                                                     (t,) if t else ())]
            if len(p) == 2:
                h = one_host(p[1])
                if not h or (t and h["tenant"] != t):
                    raise Fail(404, f"no container host {p[1]}")
                if m == "GET":
                    return 200, host_out(h)
                if m == "DELETE":
                    o = host_out(h)
                    if o["containers"] or o["stacks"]:
                        raise Fail(409, f"{h['name']} still runs {', '.join(o['containers'] + o['stacks'])}")
                    x("UPDATE hosts SET status='deleting' WHERE name=?", (h["name"],))
                    jid = new_job(dict(me, tenant=h["tenant"]), "host-delete", h["name"])
                    run_job(jid, job_delete_host, kit, me, h)
                    return 202, {"job": jid, "status_url": f"{PREFIX}/jobs/{jid}"}
        # stacks
        if p[:1] == ["stacks"]:
            t = scope(me, query)
            if len(p) == 1 and m == "GET":
                rows = q("SELECT * FROM stacks" + (" WHERE tenant=?" if t else "") + " ORDER BY tenant, name", (t,) if t else ())
                states = None
                if query.get("state") == "true":
                    states = stack_states(rows)
                return 200, [stack_out(s, states) for s in rows]
            if len(p) == 1 and m == "POST":
                if role != "consumer":
                    raise Fail(403, "stacks are created by a tenant user")
                spec = check_stack(self.body())
                if q("SELECT 1 FROM stacks WHERE tenant=? AND name=?", (me["tenant"], spec["name"])):
                    raise Fail(409, f"stack {spec['name']} exists")
                x("INSERT INTO stacks VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (me["tenant"], spec["name"], None, spec["compose"], spec["mem_mb"], json.dumps(spec["ports"]),
                   spec["expose"], "creating", None, now()))
                jid = new_job(me, "stack-create", spec["name"])
                run_job(jid, job_create_stack, kit, me, spec)
                return 202, {"job": jid, "status_url": f"{PREFIX}/jobs/{jid}"}
            if len(p) >= 2:
                s = find("stacks", me, p[1], query)
                h = one_host(s["host"]) if s["host"] else None
                if len(p) == 2 and m == "GET":
                    return 200, stack_out(s, stack_states([s]) if query.get("state") == "true" else None)
                if len(p) == 2 and m == "DELETE":
                    x("UPDATE stacks SET status='deleting' WHERE tenant=? AND name=?", (s["tenant"], s["name"]))
                    jid = new_job(dict(me, tenant=s["tenant"]), "stack-delete", s["name"])
                    run_job(jid, job_delete_stack, kit, me, s)
                    return 202, {"job": jid, "status_url": f"{PREFIX}/jobs/{jid}"}
                if p[2:] == ["logs"] and m == "GET" and h:
                    lines = max(1, min(int(query.get("tail", "100") or 100), 5000))
                    return 200, {"logs": ssh(h, ["stack", "logs", s["name"], lines], check=False)[1]}
        # containers
        t = scope(me, query)
        if not p and m == "GET":
            rows = q("SELECT * FROM containers" + (" WHERE tenant=?" if t else "") + " ORDER BY tenant, name", (t,) if t else ())
            states = live_states([one_host(h) for h in {c["host"] for c in rows if c["host"]}]) if query.get("state") == "true" else None
            return 200, [container_out(c, states) for c in rows]
        if not p and m == "POST":
            if role != "consumer":
                raise Fail(403, "containers are created by a tenant user")
            spec = check_container(self.body())
            if q("SELECT 1 FROM containers WHERE tenant=? AND name=?", (me["tenant"], spec["name"])):
                raise Fail(409, f"container {spec['name']} exists")
            x("INSERT INTO containers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (me["tenant"], spec["name"], None, spec["image"], json.dumps(spec["command"]), spec["cpus"], spec["mem_mb"],
               json.dumps(spec["env"]), json.dumps(spec["ports"]), spec["expose"], None, None, "creating", None, now()))
            jid = new_job(me, "container-create", spec["name"])
            run_job(jid, job_create_container, kit, me, spec)
            return 202, {"job": jid, "status_url": f"{PREFIX}/jobs/{jid}"}
        if p:
            c = find("containers", me, p[0], query)
            h = one_host(c["host"]) if c["host"] else None
            if len(p) == 1 and m == "GET":
                return 200, container_out(c, live_states([h]) if query.get("state") == "true" else None)
            if len(p) == 1 and m == "DELETE":
                if c["status"] in ("creating", "deleting"):
                    raise Fail(409, f"{c['name']} is {c['status']}: wait for its job")
                x("UPDATE containers SET status='deleting' WHERE tenant=? AND name=?", (c["tenant"], c["name"]))
                jid = new_job(dict(me, tenant=c["tenant"]), "container-delete", c["name"])
                run_job(jid, job_delete_container, kit, me, c)
                return 202, {"job": jid, "status_url": f"{PREFIX}/jobs/{jid}"}
            if not h or h["status"] != "ready" or c["status"] not in ("running", "stopped"):
                raise Fail(409, f"{c['name']} is {c['status']}")
            if len(p) == 2 and p[1] in ("start", "stop", "restart") and m == "POST":
                ssh(h, [p[1], c["name"]], timeout=120)
                st = "stopped" if p[1] == "stop" else "running"
                x("UPDATE containers SET status=? WHERE tenant=? AND name=?", (st, c["tenant"], c["name"]))
                return 200, container_out(dict(c, status=st))
            if p[1:] == ["logs"] and m == "GET":
                lines = max(1, min(int(query.get("tail", "100") or 100), 5000))
                return 200, {"logs": ssh(h, ["logs", c["name"], lines], check=False)[1]}
            if p[1:] == ["exec"] and m == "POST":
                if role != "consumer":
                    raise Fail(403, "exec is for the tenant's own users")
                cmd = self.body().get("command")
                if isinstance(cmd, str):
                    cmd = shlex.split(cmd)
                if not cmd or not isinstance(cmd, list) or not all(isinstance(a, str) for a in cmd):
                    raise Fail(422, "command: a list of strings, e.g. [\"ls\", \"/\"]")
                code, out = ssh(h, ["exec", c["name"], *cmd], timeout=45, check=False)
                return 200, {"exit_code": code, "output": out[-65536:]}
        raise Fail(404, "not found")


def stack_states(rows):
    out = {}
    for s in rows:
        h = one_host(s["host"]) if s["host"] else None
        if h and h["status"] == "ready":
            code, txt = ssh(h, ["stack", "ps", s["name"]], timeout=30, check=False)
            out[(h["name"], s["name"])] = [dict(zip(("name", "state", "status"), l.split("\t")))
                                           for l in (txt.splitlines() if code == 0 else []) if "\t" in l]
    return out


# ------------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="container service add-on for the vdc-cluster cloud API")
    env = os.environ.get
    ap.add_argument("--api", default=env("VDC_API", Conf.api), help="the cloud API as this host reaches it")
    ap.add_argument("--bind", default=env("VDC_CONTAINERS_BIND", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(env("VDC_CONTAINERS_PORT", "8091")))
    ap.add_argument("--state", default=env("VDC_CONTAINERS_STATE", Conf.state), help="database, SSH key, known_hosts")
    ap.add_argument("--host-flavor", default=env("VDC_CONTAINERS_FLAVOR", Conf.host_flavor), help="machine type of a new host")
    ap.add_argument("--host-image", default=env("VDC_CONTAINERS_IMAGE", Conf.host_image), help="image of a new host")
    ap.add_argument("--max-hosts", type=int, default=int(env("VDC_CONTAINERS_MAX_HOSTS", str(Conf.max_hosts))),
                    help="container hosts per tenant")
    ap.add_argument("--reserve-mb", type=int, default=int(env("VDC_CONTAINERS_RESERVE_MB", str(Conf.reserve_mb))),
                    help="MB of each host never given to containers")
    ap.add_argument("--host-ports", default=env("VDC_CONTAINERS_HOST_PORTS", "20000-20999"),
                    help="ports on a host for containers' published ports")
    ap.add_argument("--check", action="store_true", help="check the API and the SSH key, then exit")
    ap.add_argument("--quiet", action="store_true", help="no request log")
    a = ap.parse_args(argv)
    Conf.api = a.api.rstrip("/")
    Conf.state = os.path.abspath(a.state)
    Conf.host_flavor, Conf.host_image = a.host_flavor, a.host_image
    Conf.max_hosts, Conf.reserve_mb = a.max_hosts, a.reserve_mb
    lo, _, hi = a.host_ports.partition("-")
    Conf.host_ports = (int(lo), int(hi or lo))
    Conf.ssh = shlex.split(env("VDC_CONTAINERS_SSH", "ssh"))
    pub = ensure_key()
    db()
    if a.check:
        try:
            with urllib.request.urlopen(Conf.api + "/docs", timeout=10):
                pass
            print(f"ok: cloud API {Conf.api} answers; SSH key {key_path()} ({pub.split()[0]}); database {Conf.state}/containers.db")
            return 0
        except (urllib.error.URLError, OSError) as e:
            print(f"vdc-containers: the cloud API at {Conf.api} does not answer ({getattr(e, 'reason', e)})", file=sys.stderr)
            return 1
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    srv.quiet = a.quiet
    print(f"vdc-containers on http://{a.bind}:{a.port}{PREFIX}  ->  API {Conf.api}; hosts: {Conf.host_flavor} "
          f"{Conf.host_image}, at most {Conf.max_hosts} per tenant; state {Conf.state}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
