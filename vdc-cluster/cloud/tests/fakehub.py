# fakehub.py - a stand-in for the hub, so the API can be tested without the cluster.
# It replaces labhub._run and answers the hubctl commands the API sends, with the same
# output formats and registries as the real hubctl.
import shlex
from ipaddress import IPv4Network

from app.infra import labhub


class FakeHub:
    def __init__(self):
        self.tenants = {}              # name -> (vni, subnet)
        self.internet = {}             # name -> "on" | "off"
        self.keys = {}                 # tenant -> [(fingerprint, type, comment, key)]
        self.consoles = {}             # sid -> (port, guest, cred)
        self.console_port = 0          # set by the test: where the fake ttyd listens
        self.vms = {}                  # name -> dict
        self.images = {"cirros": "cirros cirros 192 1G cirros abc 20"}
        self.nodes = [("hv101", "10.20.1.101", "52:54:00:21:01:01"), ("hv102", "10.20.1.102", "52:54:00:21:01:02")]
        self.calls = []
        self.extips = {}               # ip -> (tenant, note)
        self.tports = {}               # campus port -> (proto, tenant, target, to)
        self.fips = {}                 # fip -> (tenant, target or "-")
        self.pool = [f"10.28.84.{i}" for i in range(200, 204)]

    # labhub._run replacement
    def run(self, argv, timeout=0):
        cmd = argv[-1]
        self.calls.append(cmd)
        if cmd.startswith("cat /var/lib/lab/"):
            return self.registry(cmd.split("/")[4].split()[0])
        if cmd.startswith("hubctl "):
            return self.hubctl(shlex.split(cmd)[1:])
        if "cluster.sh" in " ".join(argv):
            if "edge" in argv:
                self.edge_syncs = getattr(self, "edge_syncs", 0) + 1
                return ""
            return "VM ROLE MAC IP STATE\nhub hub 52:54:00:20:00:02 10.20.0.2 running\n"
        raise labhub.LabError("fakehub: unexpected " + cmd)

    def registry(self, name):
        if name == "tenants":
            return "".join(f"{t} {v} {s} {self.internet.get(t, 'on')}\n" for t, (v, s) in self.tenants.items())
        if name == "vms":
            return "".join(f"{n} {v['tenant']} {v['node']} {v['image']} {v['ip']} {v['mem']} {v['mac']} {v['cpus']}\n"
                           for n, v in self.vms.items())
        if name == "ports":
            return "".join(f"{n} {46000 + i}\n" for i, n in enumerate(self.vms))
        if name == "images":
            return "".join(l + "\n" for l in self.images.values())
        if name == "extips":
            return "".join(f"{a} {t} {n}\n" for a, (t, n) in self.extips.items())
        if name == "tports":
            return "".join(f"{p} {pr} {t} {g} {to}\n" for p, (pr, t, g, to) in self.tports.items())
        if name == "fips":
            return "".join(f"{f} {t} {g}\n" for f, (t, g) in self.fips.items())
        return ""

    def target_addr(self, t, g):
        if g in self.vms and self.vms[g]["tenant"] == t:
            return self.vms[g]["ip"]
        if any(v["tenant"] == t and v["ip"] == g for v in self.vms.values()):
            return g
        if self.extips.get(g, ("",))[0] == t:
            return g
        return None

    def prune(self):
        self.tports = {p: r for p, r in self.tports.items() if self.target_addr(r[1], r[2])}
        self.fips = {f: (t, g if g == "-" or self.target_addr(t, g) else "-") for f, (t, g) in self.fips.items()
                     if t in self.tenants}

    def network(self, a):
        L = labhub.LabError
        if a[:2] == ["ip", "add"]:
            t, rest = a[2], a[3:]
            note = rest[rest.index("--note") + 1] if "--note" in rest else "-"
            given = [x for x in rest if x[0].isdigit()]
            net = IPv4Network(self.tenants[t][1])
            used = {v["ip"] for v in self.vms.values() if v["tenant"] == t} | set(self.extips)
            ip = given[0] if given else next(str(h) for h in list(net.hosts())[9:] if str(h) not in used)
            if ip in used:
                raise L(f"ADDR: {ip} is already used in tenant {t}")
            self.extips[ip] = (t, note); return ip + "\n"
        if a[:2] == ["ip", "del"]:
            if a[2] not in self.extips:
                raise L(f"no extra address {a[2]} (hubctl ips)")
            del self.extips[a[2]]; self.prune(); return f"{a[2]} released\n"
        if a[:2] == ["port", "add"]:
            t, g, to = a[2], a[3], a[4]
            opt = dict(zip(a[5::2], a[6::2]))
            if not self.target_addr(t, g):
                raise L(f"{g} is not a guest or address of tenant {t} (hubctl ips {t})")
            p = opt.get("--at") or str(next(n for n in range(47000, 48000) if str(n) not in self.tports))
            if p in self.tports:
                raise L(f"campus port {p} is taken")
            self.tports[p] = (opt.get("--proto", "tcp"), t, g, to); return p + "\n"
        if a[:2] == ["port", "del"]:
            if a[2] not in self.tports:
                raise L(f"no tenant port {a[2]}")
            del self.tports[a[2]]; return "released\n"
        if a[:2] == ["port", "list"]:
            rows = ["PORT   PROTO TENANT           TARGET           ADDRESS          TO"]
            rows += [f"{p} {pr} {t} {g} {self.target_addr(t, g) or '-'} {to}" for p, (pr, t, g, to) in self.tports.items()
                     if len(a) < 3 or a[2] == t]
            return "\n".join(rows) + "\n"
        if a[0] == "fips":
            rows = ["FLOATING         TENANT           TARGET           ADDRESS"]
            for f, (t, g) in self.fips.items():
                if len(a) < 2 or a[1] == t:
                    rows.append(f"{f} {t} {g} {'-' if g == '-' else self.target_addr(t, g)}")
            if len(a) < 2:
                rows += [f"{f} - - -" for f in self.pool if f not in self.fips]
            return "\n".join(rows) + "\n"
        if a[:2] == ["fip", "alloc"]:
            free = [f for f in self.pool if f not in self.fips]
            if not free:
                raise L("the floating IP pool is used up")
            self.fips[free[0]] = (a[2], "-"); return free[0] + "\n"
        if a[:2] == ["fip", "assoc"]:
            if a[2] not in self.fips:
                raise L(f"no floating IP {a[2]}")
            t = self.fips[a[2]][0]
            addr = self.target_addr(t, a[3])
            if not addr:
                raise L(f"{a[3]} is not a guest or address of tenant {t} (hubctl ips {t})")
            for f, (ot, og) in self.fips.items():
                if f != a[2] and og != "-" and self.target_addr(ot, og) == addr:
                    raise L(f"{a[3]} ({addr}) already has floating IP {f}")
            self.fips[a[2]] = (t, a[3]); return f"{a[2]} -> {a[3]} ({addr})\n"
        if a[:2] == ["fip", "dissoc"]:
            if a[2] not in self.fips:
                raise L(f"no floating IP {a[2]}")
            self.fips[a[2]] = (self.fips[a[2]][0], "-"); return "detached\n"
        if a[:2] == ["fip", "release"]:
            if a[2] not in self.fips:
                raise L(f"no floating IP {a[2]}")
            del self.fips[a[2]]; return "back in the pool\n"
        return None

    def hubctl(self, a):
        if a and a[0] in ("ip", "port", "fip", "fips"):
            out = self.network(a)
            if out is not None:
                return out
        if a == ["nodes"]:
            rows = ["NAME     IP             MAC               STATUS    GUESTS RAM FREE/TOTAL",
                    "hub      10.20.0.2      52:54:00:20:00:02 Ready     0      1465/1977 MB"]
            for n, ip, mac in self.nodes:
                g = sum(1 for v in self.vms.values() if v["node"] == n)
                rows.append(f"{n:8} {ip:14} {mac} Ready     {g:<6} 1500/1977 MB")
            return "\n".join(rows) + "\n"
        if a[:2] == ["tenant", "add"]:
            if a[2] in self.tenants:
                raise labhub.LabError(f"tenant {a[2]} exists")
            self.tenants[a[2]] = (int(a[3]), a[4]); self.internet[a[2]] = "on"; return ""
        if a[:2] == ["tenant", "set"]:
            if a[2] not in self.tenants:
                raise labhub.LabError(f"no tenant {a[2]}")
            self.internet[a[2]] = a[4]; return f"tenant {a[2]}: internet {a[4]}\n"
        if a[:2] == ["key", "add"]:
            import base64, hashlib
            t, k = a[2], " ".join(a[3:])
            p = k.split()
            fp = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(p[1] + "=" * (-len(p[1]) % 4))).digest()).decode().rstrip("=")
            if any(x[0] == fp for x in self.keys.get(t, [])):
                raise labhub.LabError(f"key {fp} is already registered for {t}")
            self.keys.setdefault(t, []).append((fp, "ED25519" if p[0] == "ssh-ed25519" else "RSA",
                                                " ".join(p[2:]) or "-", k))
            return fp + "\n"
        if a[:2] == ["key", "del"]:
            want = a[3].removeprefix("SHA256:").replace("-", "+").replace("_", "/")
            ks = self.keys.get(a[2], [])
            left = [x for x in ks if x[0].removeprefix("SHA256:") != want]
            if len(left) == len(ks):
                raise labhub.LabError(f"no key {a[3]} for tenant {a[2]}")
            self.keys[a[2]] = left; return "removed\n"
        if a[0] == "keys":
            return "FINGERPRINT TYPE COMMENT\n" + "".join(f"{fp} {ty} {c}\n" for fp, ty, c, _ in self.keys.get(a[1], []))
        if a[:2] == ["console", "start"]:
            if a[2] not in self.vms:
                raise labhub.LabError(f"no guest {a[2]}")
            self.consoles[a[3]] = (self.console_port, a[2], a[4]); return f"{self.console_port}\n"
        if a[:2] == ["console", "stop"]:
            self.consoles.pop(a[2], None); return "stopped\n"
        if a[:2] == ["tenant", "del"]:
            if any(v["tenant"] == a[2] for v in self.vms.values()):
                raise labhub.LabError(f"tenant {a[2]} has guests")
            self.tenants.pop(a[2], None); return ""
        if a[:2] == ["vm", "add"]:
            t, name = a[2], a[3]
            opt = dict(zip(a[4::2], a[5::2]))
            if t not in self.tenants:
                raise labhub.LabError(f"no tenant {t}")
            if name.startswith("boom"):
                raise labhub.LabError("no Ready node has 512 MB free")
            net = IPv4Network(self.tenants[t][1])
            used = {v["ip"] for v in self.vms.values() if v["tenant"] == t} | {a for a, r in self.extips.items() if r[0] == t}
            ip = opt.get("--ip") or next(str(h) for h in list(net.hosts())[9:] if str(h) not in used)
            node = min(self.nodes, key=lambda n: sum(1 for v in self.vms.values() if v["node"] == n[0]))[0]
            self.vms[name] = dict(tenant=t, node=node, image=opt.get("--image", "cirros"), ip=ip,
                                  mem=opt.get("--mem", "192"), cpus=opt.get("--cpus", "1"),
                                  mac=f"52:54:01:00:00:{len(self.vms) + 1:02x}", state="running")
            return ""
        if a[:2] == ["vm", "del"]:
            if a[2] not in self.vms:
                raise labhub.LabError(f"no guest {a[2]}")
            del self.vms[a[2]]; self.prune(); return ""
        if a[:2] in (["vm", "start"], ["vm", "stop"], ["vm", "reboot"]):
            if a[2] not in self.vms:
                raise labhub.LabError(f"no guest {a[2]}")
            self.vms[a[2]]["state"] = "shut off" if a[1] == "stop" else "running"
            return f"{a[2]} {a[1]}: done\n"
        if a[:2] == ["vm", "info"]:
            return f"guest    {a[2]}\nlogin    cirros / vdclab   (hubctl vm console {a[2]})\n"
        if a == ["vms"]:
            rows = ["GUEST          TENANT       NODE     IMAGE      ADDRESS          RAM    STATE"]
            rows += [f"{n:14} {v['tenant']:12} {v['node']:8} {v['image']:10} {v['ip']:16} {v['mem']:6} {v['state']}"
                     for n, v in self.vms.items()]
            return "\n".join(rows) + "\n"
        if a and a[0] == "ips":
            rows = ["TENANT           SUBNET             ADDRESS          MAC               GUEST"]
            for t, (_, sn) in self.tenants.items():
                if len(a) > 1 and a[1] != t:
                    continue
                for n, v in self.vms.items():
                    if v["tenant"] == t:
                        rows.append(f"{t:16} {sn:18} {v['ip']:16} {v['mac']:17} {n}")
                for ip, (et, note) in self.extips.items():
                    if et == t:
                        rows.append(f"{t:16} {sn:18} {ip:16} {'-':17} (extra{':' + note if note != '-' else ''})")
                rows.append(f"{t:16} {sn:18} {'10.x.next':16} {'':17} <- next free")
            return "\n".join(rows) + "\n"
        if a[:1] == ["exec"]:
            return f"2026-11-04 08:31:02 {a[1]} [lab-agent] $ ip link add gnv199 type geneve id 199 remote 10.20.0.2\n"
        if a[:2] == ["node", "del"]:
            self.nodes = [n for n in self.nodes if n[0] != a[2]]; return ""
        raise labhub.LabError("fakehub: hubctl " + " ".join(a) + " not emulated")
