# nodes.py - cluster members (hub + spokes)
from .labhub import expose, hub, hub_read, table, check, LabError


@expose("nodes.list")
def list(**kw):
    rows = []
    for r in table(hub("nodes")):
        # last column is "1465/1977 MB"
        free, _, total = r.get("ram", "").partition("/")
        rows.append(dict(name=r["name"], ip=r["ip"], mac=r["mac"], status=r["status"],
                         guests=int(r["guests"]), ram_free_mb=_int(free),
                         ram_total_mb=_int(total.split()[0] if total else "")))
    return rows


@expose("nodes.get")
def get(name, **kw):
    check("node", name, "name")
    for n in list():
        if n["name"] == name:
            n["vms"] = [v["name"] for v in hub_read("vms") if v["node"] == name]
            return n
    raise LabError(f"no node {name}")


@expose("nodes.delete")
def delete(name, **kw):
    """evict a spoke from the hub (tunnels, DNS, SSH trust). The VM itself stays: cluster.rm"""
    check("node", name, "name")
    if name == "hub":
        raise LabError("the hub cannot be deleted")
    hub("node", "del", name)
    return dict(deleted=name)


@expose("nodes.token")
def token(**kw):
    """join token (not rotated here: the lab PC passes state/token to the hub at boot)"""
    return dict(token=hub("token").strip().split()[-1])


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None
