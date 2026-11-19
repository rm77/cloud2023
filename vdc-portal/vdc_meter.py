#!/usr/bin/env python3
"""vdc-meter: extra billing metrics for vdc-cluster, as an add-on that gets its data from the
cloud API. The kit's own metering (VM running hours at the machine type's price) stays as it is;
this meters whatever metrics.json defines, prices them, and keeps them in its own database.

    python3 vdc_meter.py                 sample every interval_minutes, forever
    python3 vdc_meter.py --once          one round (for cron or a systemd timer)
    python3 vdc_meter.py --check         one round without saving: show what would be charged
    python3 vdc_meter.py --report [--since 2026-11-01] [--until 2026-12-01] [--tenant tenant-a]

It logs in to the API as a provider account (the API has no read-only role that sees every VM):
VDC_METER_USER / VDC_METER_PASSWORD, or --user and --password-file. Python 3.8+ standard
library only. portal.py reads the same database to show the charges in the web console.
"""
import argparse
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
HOURS_PER_MONTH = 730
SOURCES = ("vm", "tunnels", "tenant", "command")
VM_VALUES = ("count", "vcpus", "mem_gb", "disk_gb")
TENANT_VALUES = ("count", "internet", "vms", "vcpus", "mem_gb", "disk_gb")


def utcnow():
    return datetime.now(timezone.utc)


def iso(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    """2026-11-01, 2026-11-01T08:00, ...Z or +07:00; without a zone it is UTC"""
    if s is None or isinstance(s, datetime):
        return s
    s = s.strip().replace("Z", "+00:00")
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------------------- configuration
def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    base = os.path.dirname(os.path.abspath(path))
    cfg.setdefault("api", os.environ.get("VDC_API", "http://127.0.0.1:36100"))
    cfg.setdefault("interval_minutes", 5)
    db = cfg.get("db", "state/meter.db")
    cfg["db"] = db if os.path.isabs(db) else os.path.join(base, db)
    names = set()
    for m in cfg.get("metrics", []):
        n = m.get("name", "")
        if not n or not n.replace("-", "").replace("_", "").isalnum() or n in names:
            raise SystemExit(f"vdc-meter: each metric needs a unique name of letters, digits, - or _ ({n!r})")
        names.add(n)
        m.setdefault("label", n)
        m.setdefault("kind", "gauge")
        m.setdefault("per", "hour")
        m.setdefault("unit", (m.get("value", "unit") + "-" + m["per"]) if m["kind"] == "gauge" else "unit")
        m.setdefault("when", "running")
        m.setdefault("enabled", True)
        src = m.get("source")
        if src not in SOURCES:
            raise SystemExit(f"vdc-meter: metric {n}: source must be one of {', '.join(SOURCES)}")
        if m["kind"] not in ("gauge", "amount"):
            raise SystemExit(f"vdc-meter: metric {n}: kind is gauge (value over time) or amount (used since last round)")
        if m["per"] not in ("hour", "month"):
            raise SystemExit(f"vdc-meter: metric {n}: per is hour or month")
        if src == "vm" and m.get("value", "count") not in VM_VALUES:
            raise SystemExit(f"vdc-meter: metric {n}: value for source vm is one of {', '.join(VM_VALUES)}")
        if src == "tenant" and m.get("value", "count") not in TENANT_VALUES:
            raise SystemExit(f"vdc-meter: metric {n}: value for source tenant is one of {', '.join(TENANT_VALUES)}")
        if src == "vm" and m["when"] not in ("running", "stopped", "always"):
            raise SystemExit(f"vdc-meter: metric {n}: when is running, stopped or always")
        if src == "command" and not m.get("command"):
            raise SystemExit(f"vdc-meter: metric {n}: source command needs a command")
        try:
            m["price"] = float(m.get("price", 0))
        except (TypeError, ValueError):
            raise SystemExit(f"vdc-meter: metric {n}: price must be a number")
    return cfg


def public_metrics(cfg):
    """what the console may show: the definitions without commands"""
    keep = ("name", "label", "unit", "kind", "per", "price", "source", "value", "when", "description")
    out = []
    for m in cfg.get("metrics", []):
        if m.get("enabled", True):
            d = {k: m[k] for k in keep if k in m}
            if m["source"] not in ("vm", "tenant"):
                d.pop("when", None)                      # only vm and tenant sources look at power state
            out.append(d)
    return out


# ------------------------------------------------------------------------------------- the API
class Api:
    def __init__(self, base, user, password):
        self.base, self.user, self.password, self.token = base.rstrip("/"), user, password, None

    def _req(self, path, data=None, headers=None):
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"null")

    def login(self):
        if not self.user or not self.password:
            raise SystemExit("vdc-meter: give a provider account: VDC_METER_USER and VDC_METER_PASSWORD "
                             "(or --user and --password-file)")
        body = urllib.parse.urlencode({"username": self.user, "password": self.password}).encode()
        try:
            self.token = self._req("/auth/token", body, {"Content-Type": "application/x-www-form-urlencoded"})["access_token"]
        except urllib.error.HTTPError as e:
            raise SystemExit(f"vdc-meter: login as {self.user} refused ({e.code})")
        me = self.get("/auth/me")
        if me.get("role") != "provider":
            raise SystemExit(f"vdc-meter: {self.user} is a {me.get('role')}; the meter needs a provider account")

    def get(self, path):
        if not self.token:
            self.login()
        try:
            return self._req(path, headers={"Authorization": "Bearer " + self.token})
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            self.login()                                    # the token expired (8 h): log in again
            return self._req(path, headers={"Authorization": "Bearer " + self.token})


# ------------------------------------------------------------------------------------ measuring
def snapshot(api):
    """everything the built-in sources need, from four API calls"""
    flavors = {f["name"]: f for f in api.get("/provider/flavors")}
    vms = api.get("/provider/vms?state=true")
    tenants = api.get("/provider/tenants")
    tunnels = api.get("/provider/tunnels")
    for v in vms:
        f = flavors.get(v["flavor"], {})
        v["disk_gb"] = f.get("disk_gb", 0)
        v["mem_gb"] = v["mem_mb"] / 1024
        v["count"] = 1
    return dict(vms=vms, tenants=tenants, tunnels=tunnels)


def vm_selected(v, when):
    if v.get("status") not in ("active", "error"):          # creating / deleting: not yet / no longer
        return False
    running = v.get("state") == "running"
    return when == "always" or (when == "running" and running) or (when == "stopped" and not running)


def measure(m, snap, api, cfg):
    """[(tenant, vm, value)] for one metric now; vm is "-" for a tenant-wide value"""
    src, out = m["source"], []
    if src == "vm":
        val = m.get("value", "count")
        out = [(v["tenant"], v["name"], float(v[val])) for v in snap["vms"] if vm_selected(v, m["when"])]
    elif src == "tunnels":
        owner = {v["name"]: v["tenant"] for v in snap["vms"]}
        n = defaultdict(int)
        for t in snap["tunnels"]:
            if m.get("protocol") and t["protocol"] != m["protocol"]:
                continue
            n[(owner.get(t["vm"], "?"), t["vm"])] += 1
        out = [(t, v, float(c)) for (t, v), c in n.items()]
    elif src == "tenant":
        val = m.get("value", "count")
        for t in snap["tenants"]:
            mine = [v for v in snap["vms"] if v["tenant"] == t["name"] and vm_selected(v, m["when"])]
            x = {"count": 1, "internet": 1 if t.get("internet") else 0, "vms": len(mine),
                 "vcpus": sum(v["vcpus"] for v in mine), "mem_gb": sum(v["mem_gb"] for v in mine),
                 "disk_gb": sum(v["disk_gb"] for v in mine)}[val]
            if x:
                out.append((t["name"], "-", float(x)))
    elif src == "command":
        env = dict(os.environ, VDC_API=cfg["api"], VDC_TOKEN=api.token or "", VDC_METRIC=m["name"],
                   VDC_INTERVAL_MINUTES=str(cfg["interval_minutes"]))
        r = subprocess.run(m["command"], shell=True, capture_output=True, text=True, timeout=m.get("timeout", 120),
                           env=env, cwd=HERE)
        if r.returncode:
            raise RuntimeError(f"command exited {r.returncode}: {r.stderr.strip()[:300]}")
        for line in r.stdout.splitlines():                   # TENANT VM VALUE per line
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            p = line.split()
            if len(p) != 3:
                raise RuntimeError(f"command printed {line!r}; expected: TENANT VM VALUE")
            out.append((p[0], p[1], float(p[2])))
    return out


def charge(m, value, minutes):
    """(quantity in the metric's unit, cost) for one value over one round"""
    if m["kind"] == "amount":                                # e.g. GB transferred since the last round
        q = value
    else:                                                    # a level held for `minutes`
        q = value * minutes / 60 / (HOURS_PER_MONTH if m["per"] == "month" else 1)
    return q, q * m["price"]


# -------------------------------------------------------------------------------------- storing
SCHEMA = """
create table if not exists round (id integer primary key, ts text not null, minutes real not null);
create index if not exists round_ts on round (ts);
create table if not exists sample (
  round_id integer not null references round(id), metric text not null, label text not null,
  unit text not null, kind text not null, tenant text not null, vm text not null,
  value real not null, quantity real not null, price real not null, cost real not null);
create index if not exists sample_round on sample (round_id);
create index if not exists sample_tenant on sample (tenant);
"""


def connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    c = sqlite3.connect(path, timeout=10)
    c.executescript(SCHEMA)
    return c


def sample_once(cfg, api, save=True, log=print):
    minutes = float(cfg["interval_minutes"])
    snap = snapshot(api)
    rows = []
    for m in cfg.get("metrics", []):
        if not m.get("enabled", True):
            continue
        try:
            vals = measure(m, snap, api, cfg)
        except Exception as e:                               # one bad metric does not stop the others
            log(f"{iso(utcnow())} metric {m['name']}: {e}")
            continue
        for tenant, vm, value in vals:
            q, cost = charge(m, value, minutes)
            rows.append((m["name"], m["label"], m["unit"], m["kind"], tenant, vm, value, q, m["price"], cost))
    if save:
        c = connect(cfg["db"])
        with c:
            rid = c.execute("insert into round (ts, minutes) values (?, ?)", (iso(utcnow()), minutes)).lastrowid
            c.executemany("insert into sample values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [(rid,) + r for r in rows])
        c.close()
    return rows


# ------------------------------------------------------------------------------------ reporting
def report(cfg, since=None, until=None, tenant=None):
    """totals per metric and VM, per day, and the current rate, for [since, until)"""
    until = parse_time(until) or (utcnow() + timedelta(seconds=1))   # rounds are stamped to the second
    since = parse_time(since) or (until - timedelta(days=30))
    out = dict(since=iso(since), until=iso(until), lines=[], metrics=[], days=[], total_cost=0.0, rate_per_hour=0.0)
    if not os.path.exists(cfg["db"]):
        return out
    c = connect(cfg["db"])
    q = ("select r.ts, s.metric, s.label, s.unit, s.kind, s.tenant, s.vm, s.quantity, s.cost "
         "from sample s join round r on r.id = s.round_id where r.ts >= ? and r.ts < ?")
    args = [iso(since), iso(until)]
    if tenant:
        q += " and s.tenant = ?"
        args.append(tenant)
    lines, metrics, days = {}, {}, defaultdict(float)
    for ts, metric, label, unit, kind, t, vm, qty, cost in c.execute(q, args):
        k = (metric, t, vm)
        ln = lines.setdefault(k, dict(metric=metric, label=label, unit=unit, tenant=t, vm=vm, quantity=0.0, cost=0.0))
        ln["quantity"] += qty
        ln["cost"] += cost
        mt = metrics.setdefault(metric, dict(metric=metric, label=label, unit=unit, quantity=0.0, cost=0.0))
        mt["quantity"] += qty
        mt["cost"] += cost
        days[ts[:10]] += cost
    # the rate now: what the newest round in the period cost per hour (gauges only)
    last = c.execute("select id, minutes from round where ts >= ? and ts < ? order by ts desc limit 1", args[:2]).fetchone()
    if last:
        q2 = "select coalesce(sum(cost), 0) from sample where round_id = ? and kind = 'gauge'"
        a2 = [last[0]]
        if tenant:
            q2 += " and tenant = ?"
            a2.append(tenant)
        out["rate_per_hour"] = round(c.execute(q2, a2).fetchone()[0] / (last[1] / 60), 6)
    c.close()
    r4 = lambda d: {**d, "quantity": round(d["quantity"], 4), "cost": round(d["cost"], 4)}
    out["lines"] = [r4(v) for _, v in sorted(lines.items())]
    out["metrics"] = [r4(v) for _, v in sorted(metrics.items())]
    out["days"] = [dict(date=k, cost=round(v, 4)) for k, v in sorted(days.items())]
    out["total_cost"] = round(sum(v["cost"] for v in lines.values()), 4)
    return out


# ------------------------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="extra billing metrics for vdc-cluster, through the cloud API")
    ap.add_argument("-c", "--config", default=os.environ.get("VDC_METER_CONFIG", os.path.join(HERE, "metrics.json")))
    ap.add_argument("--once", action="store_true", help="one round, then exit")
    ap.add_argument("--check", action="store_true", help="one round without saving; print what it would charge")
    ap.add_argument("--report", action="store_true", help="print the totals from the database")
    ap.add_argument("--since"), ap.add_argument("--until"), ap.add_argument("--tenant")
    ap.add_argument("--user", default=os.environ.get("VDC_METER_USER"))
    ap.add_argument("--password-file", help="a file holding the password (instead of VDC_METER_PASSWORD)")
    a = ap.parse_args()
    cfg = load_config(a.config)
    if a.report:
        print(json.dumps(report(cfg, a.since, a.until, a.tenant), indent=2))
        return
    pw = os.environ.get("VDC_METER_PASSWORD")
    if a.password_file:
        with open(a.password_file) as f:
            pw = f.read().strip()
    api = Api(cfg["api"], a.user, pw)
    api.login()
    if a.check:
        rows = sample_once(cfg, api, save=False)
        print(f"{'METRIC':14} {'TENANT':14} {'VM':12} {'VALUE':>10} {'QUANTITY':>12} {'UNIT':14} {'COST':>10}")
        for name, _, unit, _, t, vm, value, q, _, cost in rows:
            print(f"{name:14} {t:14} {vm:12} {value:10.3f} {q:12.6f} {unit:14} {cost:10.6f}")
        print(f"one round of {cfg['interval_minutes']} min: {sum(r[-1] for r in rows):.6f}")
        return
    names = ", ".join(m["name"] for m in cfg.get("metrics", []) if m.get("enabled", True)) or "none"
    print(f"vdc-meter: API {cfg['api']} as {a.user}, every {cfg['interval_minutes']} min, metrics: {names}; "
          f"database {cfg['db']}", flush=True)
    while True:
        t0 = time.time()
        try:
            rows = sample_once(cfg, api)
            print(f"{iso(utcnow())} sampled {len(rows)} value(s), {sum(r[-1] for r in rows):.6f}", flush=True)
        except (urllib.error.URLError, OSError) as e:        # the API is down: try again next round
            print(f"{iso(utcnow())} API not reachable: {e}", flush=True)
        if a.once:
            break
        time.sleep(max(1, cfg["interval_minutes"] * 60 - (time.time() - t0)))


if __name__ == "__main__":
    main()
