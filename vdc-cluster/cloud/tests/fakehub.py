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
        return ""

    def hubctl(self, a):
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
            fp = "SHA256:" + base64.b64encode(hashlib.sha256(p[1].encode()).digest()).decode().rstrip("=")
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
            used = {v["ip"] for v in self.vms.values() if v["tenant"] == t}
            ip = opt.get("--ip") or next(str(h) for h in list(net.hosts())[9:] if str(h) not in used)
            node = min(self.nodes, key=lambda n: sum(1 for v in self.vms.values() if v["node"] == n[0]))[0]
            self.vms[name] = dict(tenant=t, node=node, image=opt.get("--image", "cirros"), ip=ip,
                                  mem=opt.get("--mem", "192"), cpus=opt.get("--cpus", "1"),
                                  mac=f"52:54:01:00:00:{len(self.vms) + 1:02x}", state="running")
            return ""
        if a[:2] == ["vm", "del"]:
            if a[2] not in self.vms:
                raise labhub.LabError(f"no guest {a[2]}")
            del self.vms[a[2]]; return ""
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
                rows.append(f"{t:16} {sn:18} {'10.x.next':16} {'':17} <- next free")
            return "\n".join(rows) + "\n"
        if a[:1] == ["exec"]:
            return f"2026-11-04 08:31:02 {a[1]} [lab-agent] $ ip link add gnv199 type geneve id 199 remote 10.20.0.2\n"
        if a[:2] == ["node", "del"]:
            self.nodes = [n for n in self.nodes if n[0] != a[2]]; return ""
        raise labhub.LabError("fakehub: hubctl " + " ".join(a) + " not emulated")
