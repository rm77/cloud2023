# /tenant - the CCRA Cloud Consumer. Every route works inside the caller's own tenant:
# the tenant comes from the token, never from the request, and another tenant's
# objects answer 404 (they do not exist for this caller).
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session, select

from .. import metering
from ..auth import Caller, hash_password, require_role, require_tenant_admin
from ..db import Flavor, Job, User, Vm, get_session
from ..infra import images as hub_images, labhub
from ..schemas import (Accepted, FlavorModel, ImageOut, JobOut, NetworkOut, TenantOut, UsageReport, UserIn,
                       UserOut, VmIn, VmOut)
from ..services import (power_job, audit, check_quota, enqueue, gateway_of, hub_call, job_view, quota_of,
                        tenant_allocations, usage_of, vm_views)
from .. import tasks
from .. import access
from ..db import ConsoleSession, Tunnel
from ..schemas import AccessOut, ConsoleIn, ConsoleOut, KeyIn, KeyOut, TunnelIn, TunnelOut
from ..schemas import AssociateIn, ExtraIpIn, ExtraIpOut, FloatingIpIn, FloatingIpOut, PortIn, PortOut
from .. import network as netsvc

PROV = "Provisioning & configuration"
BIZ = "Business support"
PORT = "Portability & interoperability"
ACC = "Access & connectivity"

router = APIRouter(prefix="/tenant", dependencies=[Depends(require_role("consumer"))])
consumer = require_role("consumer")


def tenant_out(s, t, hubmap=None, net=None):
    hubmap = hubmap if hubmap is not None else access.hub_tenants()
    return TenantOut(name=t.name, display_name=t.display_name, vni=t.vni, subnet=t.subnet,
                     gateway=gateway_of(t.subnet), internet=access.internet_of(hubmap.get(t.name)),
                     jump_user=f"t-{t.name}", quota=quota_of(t), usage=usage_of(s, t, net), created=t.created)


def my_vm(name: str, c: Caller = Depends(consumer), s: Session = Depends(get_session)) -> Vm:
    """object-level check: a VM of another tenant is reported as not found"""
    v = s.get(Vm, name)
    if v is None or v.tenant_id != c.tenant.id:
        raise HTTPException(404, f"no VM {name} in tenant {c.tenant_name}")
    return v


def my_job(job_id: str, c: Caller = Depends(consumer), s: Session = Depends(get_session)) -> Job:
    j = s.get(Job, job_id)
    if j is None or j.tenant_id != c.tenant.id:
        raise HTTPException(404, f"no job {job_id} in tenant {c.tenant_name}")
    return j


# ---------------------------------------------------------------- business support
@router.get("/me", response_model=TenantOut, tags=[BIZ], summary="My tenant: network, quota, current usage")
def me(c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    return tenant_out(s, c.tenant)


@router.get("/flavors", response_model=list[FlavorModel], tags=[BIZ], summary="What I can order, and its price")
def flavors(s: Session = Depends(get_session)):
    return s.exec(select(Flavor).order_by(Flavor.mem_mb)).all()


@router.get("/usage", response_model=UsageReport, tags=[BIZ], summary="My metered usage and cost")
def usage(since: Optional[datetime] = None, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    return metering.report(s, since=since, tenant=c.tenant_name)


@router.get("/users", response_model=list[UserOut], tags=[BIZ], summary="Users of my tenant")
def users(c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    return [UserOut(username=u.username, role=u.role, tenant=c.tenant_name, tenant_admin=u.tenant_admin,
                    created=u.created) for u in s.exec(select(User).where(User.tenant_id == c.tenant.id))]


@router.post("/users", response_model=UserOut, status_code=201, tags=[BIZ],
             summary="Add a member to my tenant (tenant admin only)")
def add_user(body: UserIn, c: Caller = Depends(require_tenant_admin), s: Session = Depends(get_session)):
    if s.exec(select(User).where(User.username == body.username)).first():
        raise HTTPException(409, f"user {body.username} exists")
    u = User(username=body.username, password_hash=hash_password(body.password), role="consumer",
             tenant_id=c.tenant.id)
    s.add(u); s.commit(); s.refresh(u)
    audit(s, c, "user.create", body.username)
    return UserOut(username=u.username, role=u.role, tenant=c.tenant_name, created=u.created)


# ---------------------------------------------------------------- portability
@router.get("/images", response_model=list[ImageOut], tags=[PORT], summary="Images I can start a VM from")
def images():
    return hub_call(hub_images.list)


# ---------------------------------------------------------------- provisioning
@router.get("/network", response_model=NetworkOut, tags=[PROV], summary="My subnet and its address allocations")
def network(c: Caller = Depends(consumer)):
    t = c.tenant
    return NetworkOut(tenant=t.name, vni=t.vni, subnet=t.subnet, gateway=gateway_of(t.subnet),
                      allocations=tenant_allocations(t))


@router.get("/vms", response_model=list[VmOut], tags=[PROV], summary="My VMs (state=true asks the nodes: slower)")
def list_vms(state: bool = False, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    recs = s.exec(select(Vm).where(Vm.tenant_id == c.tenant.id).order_by(Vm.name)).all()
    return vm_views(s, recs, with_state=state)


@router.post("/vms", response_model=Accepted, status_code=202, tags=[PROV],
             summary="Rapid provisioning: create a VM in my tenant (address chosen by the hub)")
def create_vm(body: VmIn, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    t = c.tenant
    f = s.get(Flavor, body.flavor)
    if f is None:
        raise HTTPException(404, f"no flavor {body.flavor}")
    imgs = {i["name"]: i for i in hub_call(hub_images.list)}
    if body.image not in imgs:
        raise HTTPException(404, f"no image {body.image}")
    if imgs[body.image]["mem_mb"] > f.mem_mb:
        raise HTTPException(409, f"image {body.image} needs {imgs[body.image]['mem_mb']} MB, flavor {f.name} has {f.mem_mb}")
    if s.get(Vm, body.name) or any(r["name"] == body.name for r in hub_call(labhub.hub_read, "vms")):
        raise HTTPException(409, f"VM name {body.name} is taken (names are unique in the whole cluster)")
    try:
        check_quota(s, t, f)
        if body.floating_ip:
            u, q = usage_of(s, t), quota_of(t)
            if u.floating_ips + 1 > q.max_floating_ips:
                raise HTTPException(403, f"quota exceeded: floating ips {u.floating_ips + 1}/{q.max_floating_ips}")
    except HTTPException as e:
        audit(s, c, "vm.create", body.name, "denied", e.detail)
        raise
    rec = Vm(name=body.name, tenant_id=t.id, owner=c.username, flavor=f.name, image=body.image)
    s.add(rec); s.commit()                      # counts against the quota from now on
    try:
        j = enqueue(s, c, "vm.create", body.name, tasks.create_vm, tenant_id=t.id,
                    name=body.name, tenant=t.name, flavor=f.name, image=body.image, ip=body.ip,
                    floating_ip=body.floating_ip)
    except HTTPException:
        s.delete(rec); s.commit()               # nothing was queued: release name and quota
        raise
    rec.job_id = j.id; s.add(rec); s.commit()
    audit(s, c, "vm.create", body.name, "accepted", f"flavor {f.name}, image {body.image}"
          f"{', floating IP' if body.floating_ip else ''}, job {j.id}")
    return Accepted(job=j.id, status_url=f"/tenant/jobs/{j.id}")


@router.get("/vms/{name}", response_model=VmOut, tags=[PROV], summary="One of my VMs, with its power state")
def get_vm(v: Vm = Depends(my_vm), s: Session = Depends(get_session)):
    return vm_views(s, [v], with_state=True)[0]


@router.delete("/vms/{name}", response_model=Accepted, status_code=202, tags=[PROV],
               summary="Delete one of my VMs (its address is released)")
def delete_vm(v: Vm = Depends(my_vm), c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    if v.status == "deleting":
        raise HTTPException(409, f"{v.name} is already being deleted")
    v.status = "deleting"; s.add(v); s.commit()
    j = enqueue(s, c, "vm.delete", v.name, tasks.delete_vm, tenant_id=c.tenant.id, name=v.name)
    audit(s, c, "vm.delete", v.name, "accepted", f"job {j.id}")
    return Accepted(job=j.id, status_url=f"/tenant/jobs/{j.id}")


# ---- power: start, stop (ACPI, then forced after a minute), reboot. A VM stopped here stays off
# when its node restarts; every other VM is started again.
@router.post("/vms/{name}/start", response_model=Accepted, status_code=202, tags=[PROV], summary="Start one of my VMs")
def start_vm(v: Vm = Depends(my_vm), c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    return power_job(s, c, v, "start", "/tenant/jobs")


@router.post("/vms/{name}/stop", response_model=Accepted, status_code=202, tags=[PROV],
             summary="Stop one of my VMs (shut down; it stays off until started)")
def stop_vm(v: Vm = Depends(my_vm), c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    return power_job(s, c, v, "stop", "/tenant/jobs")


@router.post("/vms/{name}/reboot", response_model=Accepted, status_code=202, tags=[PROV], summary="Reboot one of my VMs")
def reboot_vm(v: Vm = Depends(my_vm), c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    return power_job(s, c, v, "reboot", "/tenant/jobs")


@router.get("/jobs", response_model=list[JobOut], tags=[PROV], summary="My jobs, newest first")
def jobs(limit: int = 50, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    rows = s.exec(select(Job).where(Job.tenant_id == c.tenant.id).order_by(Job.created.desc()).limit(min(limit, 500)))
    return [job_view(s, j) for j in rows]


@router.get("/jobs/{job_id}", response_model=JobOut, tags=[PROV], summary="One of my jobs")
def job(j: Job = Depends(my_job), s: Session = Depends(get_session)):
    return job_view(s, j)


# ---------------------------------------------------------------- access & connectivity
@router.get("/access", response_model=AccessOut, tags=[ACC],
            summary="How to reach my guests: gateway, internet, jump host, ssh commands, ~/.ssh/config")
def my_access(c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    return access.access_of(s, c.tenant)


@router.get("/keys", response_model=list[KeyOut], tags=[ACC], summary="My tenant's SSH public keys")
def keys(c: Caller = Depends(consumer)):
    return access.list_keys(c.tenant)


@router.post("/keys", response_model=KeyOut, status_code=201, tags=[ACC],
             summary="Add an SSH public key: jump host login, and put into guests created from now on")
def add_key(body: KeyIn, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    return access.add_key(s, c, c.tenant, body.key)


@router.delete("/keys/{key_id}", status_code=204, tags=[ACC], summary="Remove an SSH key (id from GET /tenant/keys)")
def delete_key(key_id: str, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    access.delete_key(s, c, c.tenant, key_id)
    return Response(status_code=204)


@router.post("/vms/{name}/console", response_model=ConsoleOut, status_code=201, tags=[ACC],
             summary="Web console of one of my VMs: open the returned url in a browser "
                     "({\"public\": true}: also a public_url through a Cloudflare tunnel)")
def open_console(body: ConsoleIn | None = None, v: Vm = Depends(my_vm), c: Caller = Depends(consumer),
                 s: Session = Depends(get_session)):
    if v.status != "active":
        raise HTTPException(409, f"{v.name} is {v.status}")
    return access.open_console(s, c, v, public=bool(body and body.public))


@router.get("/consoles", response_model=list[ConsoleOut], tags=[ACC], summary="My open web consoles")
def consoles(c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    access.expire_consoles(s)
    rows = s.exec(select(ConsoleSession).where(ConsoleSession.tenant_id == c.tenant.id,
                                               ConsoleSession.closed == False))  # noqa: E712
    return [access.console_out(s, r) for r in rows]


@router.delete("/consoles/{sid}", status_code=204, tags=[ACC], summary="Close one of my web consoles")
def close_console(sid: str, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    cs = s.get(ConsoleSession, sid)
    if cs is None or cs.tenant_id != c.tenant.id:
        raise HTTPException(404, "no such console in your tenant")
    access.close_console(s, c, cs)
    return Response(status_code=204)


# ---------------------------------------------------------------- Cloudflare quick tunnels
@router.post("/vms/{name}/tunnels", response_model=TunnelOut, status_code=201, tags=[ACC],
             summary="Publish a port of my VM on the internet: https://<random>.trycloudflare.com")
def open_tunnel(body: TunnelIn, v: Vm = Depends(my_vm), c: Caller = Depends(consumer),
                s: Session = Depends(get_session)):
    return access.open_tunnel(s, c, v, body.protocol, body.port, body.hours)


@router.get("/tunnels", response_model=list[TunnelOut], tags=[ACC], summary="My open tunnels")
def my_tunnels(c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    access.expire_tunnels(s)
    rows = s.exec(select(Tunnel).where(Tunnel.tenant_id == c.tenant.id, Tunnel.closed == False))  # noqa: E712
    return [access.tunnel_out(s, t) for t in rows]


@router.delete("/tunnels/{tid}", status_code=204, tags=[ACC], summary="Close one of my tunnels")
def close_tunnel(tid: str, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    t = s.get(Tunnel, tid)
    if t is None or t.tenant_id != c.tenant.id or t.closed:
        raise HTTPException(404, "no such tunnel in your tenant")
    access.close_tunnel(s, c, t)
    return Response(status_code=204)


# ---------------------------------------------------------------- extra addresses, campus ports, floating IPs
# Kept by the hub; the lab PC forwards them. A target is one of my VMs or an address of my subnet.
@router.get("/ips", response_model=list[ExtraIpOut], tags=[ACC],
            summary="My extra addresses: in my subnet, given to no VM (service addresses, e.g. MetalLB)")
def my_ips(c: Caller = Depends(consumer)):
    return netsvc.list_ips(c.tenant_name)


@router.post("/ips", response_model=ExtraIpOut, status_code=201, tags=[ACC],
             summary="Reserve an extra address of my subnet (no DHCP hands it out)")
def add_ip(body: ExtraIpIn | None = None, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    body = body or ExtraIpIn()
    return netsvc.add_ip(s, c, c.tenant, body.ip, body.note)


@router.delete("/ips/{ip}", status_code=204, tags=[ACC],
               summary="Release an extra address (its campus ports and floating IP let go of it)")
def delete_ip(ip: str, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    netsvc.delete_ip(s, c, c.tenant, ip)
    return Response(status_code=204)


@router.get("/ports", response_model=list[PortOut], tags=[ACC], summary="My campus ports")
def my_ports(c: Caller = Depends(consumer)):
    return netsvc.list_ports(c.tenant_name)


@router.post("/ports", response_model=PortOut, status_code=201, tags=[ACC],
             summary="Open a port of my VM (or address) on the lab PC's campus address: connect to <lab PC>:campus_port")
def add_port(body: PortIn, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    return netsvc.add_port(s, c, c.tenant, body.target, body.port, body.protocol, body.campus_port)


@router.delete("/ports/{campus_port}", status_code=204, tags=[ACC], summary="Close one of my campus ports")
def delete_port(campus_port: int, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    netsvc.delete_port(s, c, c.tenant, campus_port)
    return Response(status_code=204)


@router.get("/floating-ips", response_model=list[FloatingIpOut], tags=[ACC], summary="My floating IPs")
def my_fips(c: Caller = Depends(consumer)):
    return netsvc.list_fips(c.tenant_name)


@router.post("/floating-ips", response_model=FloatingIpOut, status_code=201, tags=[ACC],
             summary="Book a floating IP (a campus address), and attach it to a VM or address if target is given")
def alloc_fip(body: FloatingIpIn | None = None, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    return netsvc.alloc_fip(s, c, c.tenant, (body or FloatingIpIn()).target)


@router.post("/floating-ips/{fip}/associate", response_model=FloatingIpOut, tags=[ACC],
             summary="Attach my floating IP to a VM or address: every port, and its own traffic leaves from it")
def associate_fip(fip: str, body: AssociateIn, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    return netsvc.associate_fip(s, c, c.tenant, fip, body.target)


@router.post("/floating-ips/{fip}/disassociate", response_model=FloatingIpOut, tags=[ACC],
             summary="Detach my floating IP (it stays booked)")
def disassociate_fip(fip: str, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    return netsvc.disassociate_fip(s, c, c.tenant, fip)


@router.delete("/floating-ips/{fip}", status_code=204, tags=[ACC], summary="Give my floating IP back to the pool")
def release_fip(fip: str, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    netsvc.release_fip(s, c, c.tenant, fip)
    return Response(status_code=204)
