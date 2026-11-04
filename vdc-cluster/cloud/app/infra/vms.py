# vms.py - tenant guests (L2 VMs on the spokes)
from .labhub import expose, hub, hub_read, table, check, LabError


@expose("vms.list")
def list(tenant=None, node=None, state="no", **kw):
    """GET ?tenant=tenant-a&node=hv101&state=yes  (state=yes asks every node: slower)"""
    rows = hub_read("vms")
    if tenant:
        rows = [r for r in rows if r["tenant"] == tenant]
    if node:
        rows = [r for r in rows if r["node"] == node]
    states = {}
    if str(state).lower() in ("1", "yes", "true"):
        states = {r["guest"]: r["state"] for r in table(hub("vms"))}
    return [_vm(r, states.get(r["name"])) for r in rows]


@expose("vms.get")
def get(name, **kw):
    check("name", name)
    for r in hub_read("vms"):
        if r["name"] == name:
            info = {}
            for line in hub("vm", "info", name).splitlines():
                k, _, v = line.partition(" ")
                info[k] = v.strip()
            vm = _vm(r)
            vm["login"] = info.get("login", "").split(" (")[0]
            return vm
    raise LabError(f"no guest {name}")


@expose("vms.create", async_only=True)
def create(tenant, name, image=None, node=None, ip=None, mem=None, cpus=None, disk=None, **kw):
    """POST /controllersvc/vms.create/async
       {"tenant":"tenant-a","name":"a1","image":"ubuntu","mem":1024}
       address and node are chosen by the hub unless given"""
    args = ["vm", "add", check("name", tenant, "tenant"), check("name", name)]
    if image:
        args += ["--image", check("name", image, "image")]
    if node:
        args += ["--node", check("node", node, "node")]
    if ip:
        args += ["--ip", check("ip", ip)]
    if mem:
        args += ["--mem", check("num", mem, "mem")]
    if cpus:
        args += ["--cpus", check("num", cpus, "cpus")]
    if disk:
        args += ["--disk", check("size", disk, "disk")]
    hub(*args)
    return get(name)


@expose("vms.delete")
def delete(name, **kw):
    check("name", name)
    hub("vm", "del", name)
    return dict(deleted=name, address="released")


def _vm(r, state=None):
    vm = dict(name=r["name"], tenant=r["tenant"], node=r["node"], image=r["image"] or "cirros",
              ip=r["ip"], mac=r["mac"], mem_mb=int(r["mem"] or 192), cpus=int(r["cpus"] or 1))
    if state is not None:
        vm["state"] = state
    return vm
