# labhub.py - how the management layer talks to vdc-cluster
#
# Everything goes through two doors, both already in the kit:
#   hub(...)      ssh root@hub hubctl ARGS...   (tenants, VMs, images, nodes)
#   cluster(...)  sudo cluster.sh ARGS...       (lab PC side: spoke VMs, list)
# Reads come from the hub's registry files (/var/lib/lab/*), which have fixed columns;
# changes go through hubctl, so the hub keeps doing its own locking, IPAM and scheduling.
import os
import re
import shlex
import subprocess

# default: this file is <kit>/cloud/app/infra/labhub.py
KIT = os.environ.get("VDC_KIT", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
HUB_IP = os.environ.get("VDC_HUB", "10.20.0.2")
KEY = os.environ.get("VDC_KEY", os.path.join(KIT, "state", "id_lab"))
TIMEOUT = int(os.environ.get("VDC_TIMEOUT", "900"))
SUDO = os.environ.get("VDC_SUDO", "" if os.geteuid() == 0 else "sudo -n").split()

SSH = ["ssh", "-F", "/dev/null", "-i", KEY, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
       "-o", "StrictHostKeyChecking=accept-new",
       "-o", "UserKnownHostsFile=" + os.path.join(KIT, "state", "known_hosts.mgmt"),
       "-o", "LogLevel=ERROR"]

# registry layouts (hub/rootfs/usr/local/sbin/hubctl)
REGISTRY = {
    "nodes":   ["name", "ip", "mac", "secret"],
    "tenants": ["name", "vni", "subnet", "internet"],
    "vms":     ["name", "tenant", "node", "image", "ip", "mem", "mac", "cpus"],
    "images":  ["name", "type", "mem", "disk", "user", "sha256", "size_mb"],
    "ports":   ["name", "port"],                  # campus SSH port per guest (cluster.sh edge)
}


class LabError(Exception):
    pass


class LabUnreachable(LabError):
    """the hub or the tool could not be reached at all (not: the hub refused)"""


# ---------------------------------------------------------------- exposed functions
# callit() in app.py runs only functions registered here, never an arbitrary module.func
EXPOSED = {}


def expose(name, async_only=False):
    def reg(f):
        EXPOSED[name] = dict(func=f, async_only=async_only)
        return f
    return reg


# ---------------------------------------------------------------- input checks
# same rules as the kit's lib.sh, so a bad value is refused here, not half-way in hubctl
_RULES = {
    "node":   r"^[a-z][a-z0-9]{0,7}$",
    "name":   r"^[a-z][a-z0-9-]{0,15}$",          # tenant, guest, image
    "vni":    r"^[0-9]{1,5}$",
    "cidr":   r"^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$",
    "ip":     r"^([0-9]{1,3}\.){3}[0-9]{1,3}$",
    "num":    r"^[0-9]{1,6}$",
    "size":   r"^[0-9]{1,5}[MG]$",
    "url":    r"^https?://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]+$",
    "type":   r"^(cloud-init|cirros|none)$",
    "login":  r"^[a-z_][a-z0-9_-]{0,31}$",
}


def check(kind, value, field=None):
    v = str(value)
    if not re.match(_RULES[kind], v):
        raise LabError(f"invalid {field or kind}: {v!r}")
    if kind == "vni" and not 1 <= int(v) <= 65535:
        raise LabError(f"invalid {field or 'vni'}: {v} (1-65535)")
    return v


# ---------------------------------------------------------------- the two doors
def _run(argv, timeout=TIMEOUT):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise LabUnreachable(f"timeout after {timeout}s: {shlex.join(argv)}")
    except OSError as e:                       # ssh / sudo missing on this machine
        raise LabUnreachable(f"cannot run {argv[0]}: {e.strerror}")
    if p.returncode == 255 and argv[0] == "ssh":       # ssh itself failed: no route, refused, bad key
        raise LabUnreachable("hub not reachable over SSH: " + (p.stderr.strip().splitlines() or ["?"])[-1])
    if p.returncode != 0:
        msg = (p.stderr.strip() or p.stdout.strip()).splitlines()
        raise LabError(msg[-1] if msg else f"rc={p.returncode}")
    return p.stdout


def hub(*args, timeout=TIMEOUT):
    """hubctl ARGS on the hub; every argument is quoted for the remote shell"""
    return _run(SSH + ["root@" + HUB_IP, "hubctl " + shlex.join(str(a) for a in args)], timeout)


def hub_read(registry):
    """one of the hub's registry files as a list of dicts"""
    out = _run(SSH + ["root@" + HUB_IP, "cat /var/lib/lab/" + registry + " 2>/dev/null || true"], 30)
    cols = REGISTRY[registry]
    rows = []
    for line in out.splitlines():
        f = line.split()
        if f:
            rows.append(dict(zip(cols, f + [""] * (len(cols) - len(f)))))
    return rows


def cluster(*args, timeout=TIMEOUT):
    """cluster.sh ARGS on this (lab) PC"""
    return _run(SUDO + [os.path.join(KIT, "cluster.sh")] + [str(a) for a in args], timeout)


def table(text):
    """hubctl/cluster.sh column output -> list of dicts (header row = keys)"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    keys = [k.lower() for k in lines[0].split()]
    return [dict(zip(keys, ln.split(None, len(keys) - 1))) for ln in lines[1:]]
