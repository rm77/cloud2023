# cluster.py - the lab PC side: hub/spoke VMs started by cluster.sh
import re
from .labhub import expose, cluster, table, check, LabError

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@expose("cluster.status")
def status(**kw):
    """the VMs on this PC (cluster.sh list, first table)"""
    out = ANSI.sub("", cluster("list", timeout=60))
    first = out.split("\n\n")[0].splitlines()
    vms = table("\n".join(ln for ln in first if not ln.startswith("host ")))
    return dict(host=first[0] if first else "", vms=vms)


@expose("cluster.spoke_add", async_only=True)
def spoke_add(count=1, **kw):
    """POST /controllersvc/cluster.spoke_add/async {"count":2}
       clones spokes on this PC and waits until they joined the hub"""
    n = check("num", count, "count")
    if not 1 <= int(n) <= 20:
        raise LabError("count must be 1-20")
    out = ANSI.sub("", cluster("spoke", "add", n, timeout=300 + 120 * int(n)))
    return dict(log=out.splitlines()[-15:])


@expose("cluster.start")
def start(name, **kw):
    cluster("start", check("node", name, "name"), timeout=120)
    return dict(started=name)


@expose("cluster.stop")
def stop(name, **kw):
    cluster("stop", check("node", name, "name"), timeout=120)
    return dict(stopped=name)


@expose("cluster.rm", async_only=True)
def rm(name, **kw):
    """delete a VM of this PC (a spoke is evicted from the hub first)"""
    check("node", name, "name")
    if name == "hub":
        raise LabError("refusing to delete the hub through the API")
    cluster("rm", name, timeout=180)
    return dict(removed=name)


def mtu_check(**kw):
    """underlay check: a non-fragmentable ping of the full underlay MTU to the hub"""
    try:
        out = ANSI.sub("", cluster("mtu-check", timeout=60))
        return dict(ok=True, log=out.splitlines()[-10:])
    except LabError as e:
        return dict(ok=False, log=[str(e)])
