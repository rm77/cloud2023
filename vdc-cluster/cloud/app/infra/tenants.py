# tenants.py - tenant networks (VNI + subnet) and their address allocations
from .labhub import expose, hub, hub_read, check, LabError


@expose("tenants.list")
def list(**kw):
    vms = hub_read("vms")
    out = []
    for t in hub_read("tenants"):
        out.append(dict(name=t["name"], vni=int(t["vni"]), subnet=t["subnet"],
                        guests=sum(1 for v in vms if v["tenant"] == t["name"])))
    return out


@expose("tenants.get")
def get(name, **kw):
    check("name", name)
    for t in list():
        if t["name"] == name:
            t["ips"] = ips(name)
            return t
    raise LabError(f"no tenant {name}")


@expose("tenants.create")
def create(name, vni, subnet=None, **kw):
    """POST {"name":"tenant-a","vni":199,"subnet":"10.199.20.0/24"}"""
    args = ["tenant", "add", check("name", name), check("vni", vni)]
    if subnet:
        args.append(check("cidr", subnet, "subnet"))
    hub(*args)
    return get(name)


@expose("tenants.delete")
def delete(name, **kw):
    check("name", name)
    hub("tenant", "del", name)
    return dict(deleted=name)


@expose("tenants.ips")
def ips(name=None, **kw):
    """allocations of one tenant (or all) plus the next free address"""
    args = ["ips"] + ([check("name", name)] if name else [])
    rows = []
    for line in hub(*args).splitlines()[1:]:   # TENANT SUBNET ADDRESS MAC GUEST
        f = line.split()
        if len(f) < 3:
            continue
        if "<-" in f:                          # "tenant subnet 10.x.y.z  <- next free" (no MAC)
            rows.append(dict(tenant=f[0], subnet=f[1], ip=f[2], mac=None, guest=None, next_free=True))
        else:
            rows.append(dict(tenant=f[0], subnet=f[1], ip=f[2], mac=f[3], guest=f[4], next_free=False))
    return rows
