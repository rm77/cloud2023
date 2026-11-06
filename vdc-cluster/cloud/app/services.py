# services.py - the rules between the routes and the cluster:
# audit log, quotas (contract), address pool (onboarding), jobs, the merged VM view.
from ipaddress import IPv4Network

from fastapi import HTTPException
from sqlmodel import Session, select

from . import config
from .db import AuditEvent, Flavor, Job, Tenant, Vm
from .infra import labhub, vms as hub_vms, tenants as hub_tenants
from .schemas import Quota, Usage, VmOut

# ---------------------------------------------------------------- errors from the cluster
def hub_call(fn, *a, **kw):
    """run an infra function; a refusal by hubctl becomes a 409 with its message"""
    try:
        return fn(*a, **kw)
    except labhub.LabUnreachable as e:
        raise HTTPException(502, f"cluster: {e}")
    except labhub.LabError as e:                 # hubctl refused: its message is the explanation
        msg = str(e)
        raise HTTPException(404 if msg.startswith("no ") else 409, f"cluster: {msg}")


# ---------------------------------------------------------------- audit (CCRA reporting and auditing)
def audit(s: Session, caller, action, target="", result="ok", detail="", tenant=None):
    s.add(AuditEvent(username=caller.username, role=caller.role,
                     tenant=tenant if tenant is not None else caller.tenant_name,
                     action=action, target=str(target), result=result, detail=str(detail)[:500]))
    s.commit()


# ---------------------------------------------------------------- quotas (CCRA contract management)
def quota_of(t: Tenant) -> Quota:
    return Quota(max_vms=t.max_vms, max_vcpus=t.max_vcpus, max_mem_mb=t.max_mem_mb, max_disk_gb=t.max_disk_gb,
                 max_tunnels=t.max_tunnels if t.max_tunnels is not None else 2)


def usage_of(s: Session, t: Tenant) -> Usage:
    """counted from the API's VM records, so VMs still being created already count"""
    from .db import Tunnel
    u = Usage(vms=0, vcpus=0, mem_mb=0, disk_gb=0,
              tunnels=len(s.exec(select(Tunnel).where(Tunnel.tenant_id == t.id, Tunnel.closed == False)).all()))  # noqa: E712
    flavors = {f.name: f for f in s.exec(select(Flavor))}
    hub_rows = None
    for v in s.exec(select(Vm).where(Vm.tenant_id == t.id, Vm.status != "error")):
        f = flavors.get(v.flavor)
        u.vms += 1
        if f:
            u.vcpus += f.vcpus; u.mem_mb += f.mem_mb; u.disk_gb += f.disk_gb
        else:                                   # imported VM: its size as the hub has it
            if hub_rows is None:
                hub_rows = {r["name"]: r for r in hub_call(labhub.hub_read, "vms")}
            h = hub_rows.get(v.name, {})
            u.vcpus += int(h.get("cpus") or 1); u.mem_mb += int(h.get("mem") or 0)
    return u


def check_quota(s: Session, t: Tenant, f: Flavor):
    u, q = usage_of(s, t), quota_of(t)
    over = []
    if u.vms + 1 > q.max_vms: over.append(f"VMs {u.vms + 1}/{q.max_vms}")
    if u.vcpus + f.vcpus > q.max_vcpus: over.append(f"vCPUs {u.vcpus + f.vcpus}/{q.max_vcpus}")
    if u.mem_mb + f.mem_mb > q.max_mem_mb: over.append(f"RAM {u.mem_mb + f.mem_mb}/{q.max_mem_mb} MB")
    if u.disk_gb + f.disk_gb > q.max_disk_gb: over.append(f"disk {u.disk_gb + f.disk_gb}/{q.max_disk_gb} GB")
    if over:
        raise HTTPException(403, "quota exceeded: " + ", ".join(over))


# ---------------------------------------------------------------- address pool (onboarding)
def pick_network(s: Session, vni=None, subnet=None):
    """VNI and subnet for a new tenant: given ones are checked, missing ones come from the pool"""
    hub_rows = hub_call(labhub.hub_read, "tenants")
    used_vni = {int(r["vni"]) for r in hub_rows} | {t.vni for t in s.exec(select(Tenant))}
    used_net = [IPv4Network(r["subnet"], strict=False) for r in hub_rows if r["subnet"]] + \
               [IPv4Network(t.subnet) for t in s.exec(select(Tenant))]
    if vni is None:
        free = [n for n in range(config.VNI_MIN, config.VNI_MAX + 1) if n not in used_vni]
        if not free:
            raise HTTPException(409, f"VNI pool {config.VNI_MIN}-{config.VNI_MAX} is used up")
        vni = free[0]
    elif vni in used_vni:
        raise HTTPException(409, f"VNI {vni} is already in use")
    if subnet is None:
        try:
            subnet = IPv4Network(config.SUBNET_TEMPLATE.format(vni=vni))
        except ValueError:
            raise HTTPException(409, f"no subnet for VNI {vni} from {config.SUBNET_TEMPLATE}: give one")
    clash = [str(n) for n in used_net if n.overlaps(subnet)]
    if clash:
        raise HTTPException(409, f"subnet {subnet} overlaps {', '.join(clash)}")
    if IPv4Network("10.20.0.0/16").overlaps(subnet):
        raise HTTPException(409, f"subnet {subnet} overlaps the underlay 10.20.0.0/16")
    return vni, str(subnet)


def gateway_of(subnet: str) -> str:
    return str(IPv4Network(subnet)[1])


# ---------------------------------------------------------------- jobs
# Every job is a row in the Job table (who, what, state, result). The queue that hands it to the
# worker is Redis + RQ by default (config.QUEUE_BACKEND = "redis"), or the table itself ("sqlite").
_redis = None


def redis_conn():
    global _redis
    if _redis is None:
        import redis
        _redis = redis.from_url(config.REDIS_URL)
    return _redis


def rq_queue():
    from rq import Queue
    return Queue(config.QUEUE, connection=redis_conn(), default_timeout=3600)


def enqueue(s: Session, caller, kind, target, func, tenant_id=None, **kwargs) -> Job:
    import json
    import uuid
    from . import tasks
    name = tasks.name_of(func)
    jid = f"{kind.replace('.', '-')}-{uuid.uuid4().hex[:12]}"
    j = Job(id=jid, kind=kind, target=str(target), tenant_id=tenant_id, username=caller.username,
            func=name, kwargs=json.dumps(kwargs, default=str))
    s.add(j); s.commit(); s.refresh(j)
    if config.QUEUE_BACKEND == "redis":
        try:
            rq_queue().enqueue("app.worker.run_job", jid, job_id=jid, result_ttl=86400, failure_ttl=7 * 86400)
        except Exception as e:                   # Redis down: the job never reaches a worker
            s.delete(j); s.commit()
            raise HTTPException(503, f"job queue (Redis) is not reachable: {e.__class__.__name__}")
    return j


def job_view(s: Session, j: Job) -> dict:
    import json
    t = s.get(Tenant, j.tenant_id) if j.tenant_id else None
    result = None
    if j.state == "finished" and j.result:
        r = json.loads(j.result)
        result = r if isinstance(r, (dict, list)) else {"value": r}
    return dict(id=j.id, kind=j.kind, target=j.target, username=j.username, created=j.created,
                tenant=t.name if t else None, state=j.state, ended=j.ended, result=result,
                error=j.error or None)


# ---------------------------------------------------------------- VM view (API record + hub facts)
def vm_views(s: Session, records: list[Vm], with_node=False, with_state=False) -> list[VmOut]:
    hub_rows = {r["name"]: r for r in hub_call(labhub.hub_read, "vms")}
    ports = guest_ports()
    users = {}
    if ports and records:
        users = {r["name"]: r["user"] for r in hub_call(labhub.hub_read, "images")}
    states = {}
    if with_state and records:
        states = {r["guest"]: r["state"] for r in hub_call(lambda: labhub.table(labhub.hub("vms")))}
    tenants = {t.id: t.name for t in s.exec(select(Tenant))}
    flavors = {f.name: f for f in s.exec(select(Flavor))}
    out = []
    for v in records:
        h = hub_rows.get(v.name, {})
        f = flavors.get(v.flavor)
        out.append(VmOut(name=v.name, tenant=tenants.get(v.tenant_id, "?"), flavor=v.flavor, image=v.image,
                         status=v.status, ip=h.get("ip") or None, mac=h.get("mac") or None,
                         ssh_port=ports.get(v.name), ssh=ssh_direct(ports.get(v.name), users.get(v.image, "root")),
                         vcpus=f.vcpus if f else int(h.get("cpus") or 1),
                         mem_mb=f.mem_mb if f else int(h.get("mem") or 0),
                         node=h.get("node") if with_node else None,
                         state=states.get(v.name) if with_state else None,
                         error=v.error, owner=v.owner, created=v.created))
    return out


def guest_ports() -> dict:
    """guest -> its SSH port on the lab PC's campus address (empty when the edge is off)"""
    if not config.EDGE:
        return {}
    return {r["name"]: int(r["port"]) for r in hub_call(labhub.hub_read, "ports") if r["port"].isdigit()}


def ssh_direct(port, user) -> str | None:
    return f"ssh -p {port} {user}@{config.EDGE_HOST or '<lab PC>'}" if port else None


def refresh_edge():
    """the main PC's forwards and routes (cluster.sh edge sync), after tenants or guests change"""
    try:
        labhub.cluster("edge", "sync", timeout=60)
    except labhub.LabError:
        pass


def tenant_allocations(t: Tenant):
    rows = hub_call(hub_tenants.ips, t.name)
    return [dict(ip=r["ip"], mac=r["mac"], vm=r["guest"], next_free=r["next_free"]) for r in rows]


__all__ = ["hub_call", "audit", "quota_of", "usage_of", "check_quota", "pick_network", "gateway_of",
           "enqueue", "job_view", "vm_views", "tenant_allocations", "hub_vms", "guest_ports", "ssh_direct",
           "refresh_edge"]
