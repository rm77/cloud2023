# services.py - the rules between the routes and the cluster:
# audit log, quotas (contract), address pool (onboarding), jobs, the merged VM view.
from ipaddress import IPv4Network

import redis
from fastapi import HTTPException
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job as RqJob
from sqlmodel import Session, select

from . import config
from .db import AuditEvent, Flavor, Job, Tenant, Vm
from .infra import labhub, vms as hub_vms, tenants as hub_tenants
from .schemas import Quota, Usage, VmOut

_redis = None


def queue() -> Queue:
    global _redis
    if _redis is None:
        _redis = redis.from_url(config.REDIS_URL)
    return Queue(config.QUEUE, connection=_redis, default_timeout=3600)


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
    return Quota(max_vms=t.max_vms, max_vcpus=t.max_vcpus, max_mem_mb=t.max_mem_mb, max_disk_gb=t.max_disk_gb)


def usage_of(s: Session, t: Tenant) -> Usage:
    """counted from the API's VM records, so VMs still being created already count"""
    u = Usage(vms=0, vcpus=0, mem_mb=0, disk_gb=0)
    flavors = {f.name: f for f in s.exec(select(Flavor))}
    for v in s.exec(select(Vm).where(Vm.tenant_id == t.id, Vm.status != "error")):
        f = flavors.get(v.flavor)
        u.vms += 1
        if f:
            u.vcpus += f.vcpus; u.mem_mb += f.mem_mb; u.disk_gb += f.disk_gb
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
def enqueue(s: Session, caller, kind, target, func, tenant_id=None, **kwargs) -> Job:
    import uuid
    jid = f"{kind.replace('.', '-')}-{uuid.uuid4().hex[:12]}"
    try:
        queue().enqueue(func, job_id=jid, kwargs=kwargs, result_ttl=7 * 86400, failure_ttl=7 * 86400)
    except redis.exceptions.ConnectionError:
        raise HTTPException(503, "job queue (Redis) is not reachable")
    j = Job(id=jid, kind=kind, target=target, tenant_id=tenant_id, username=caller.username)
    s.add(j); s.commit(); s.refresh(j)
    return j


def job_view(s: Session, j: Job) -> dict:
    out = dict(id=j.id, kind=j.kind, target=j.target, username=j.username, created=j.created,
               tenant=(s.get(Tenant, j.tenant_id).name if j.tenant_id and s.get(Tenant, j.tenant_id) else None),
               state="unknown", ended=None, result=None, error=None)
    try:
        r = RqJob.fetch(j.id, connection=queue().connection)
        st = r.get_status()
        out["state"] = getattr(st, "value", str(st))
        out["ended"] = r.ended_at
        if out["state"] == "finished":
            res = r.return_value()
            out["result"] = res if isinstance(res, (dict, list)) else {"value": res}
        elif out["state"] == "failed":
            res = r.latest_result() if hasattr(r, "latest_result") else None
            text = getattr(res, "exc_string", None) or getattr(r, "exc_info", "") or ""
            lines = text.strip().splitlines()
            out["error"] = lines[-1] if lines else "failed"
    except (NoSuchJobError, redis.exceptions.ConnectionError):
        pass
    return out


# ---------------------------------------------------------------- VM view (API record + hub facts)
def vm_views(s: Session, records: list[Vm], with_node=False, with_state=False) -> list[VmOut]:
    hub_rows = {r["name"]: r for r in hub_call(labhub.hub_read, "vms")}
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
                         vcpus=f.vcpus if f else int(h.get("cpus") or 1),
                         mem_mb=f.mem_mb if f else int(h.get("mem") or 0),
                         node=h.get("node") if with_node else None,
                         state=states.get(v.name) if with_state else None,
                         error=v.error, owner=v.owner, created=v.created))
    return out


def tenant_allocations(t: Tenant):
    rows = hub_call(hub_tenants.ips, t.name)
    return [dict(ip=r["ip"], mac=r["mac"], vm=r["guest"], next_free=r["next_free"]) for r in rows]


__all__ = ["hub_call", "audit", "quota_of", "usage_of", "check_quota", "pick_network", "gateway_of",
           "enqueue", "job_view", "vm_views", "tenant_allocations", "hub_vms"]
