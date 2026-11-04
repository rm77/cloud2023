# /tenant - the CCRA Cloud Consumer. Every route works inside the caller's own tenant:
# the tenant comes from the token, never from the request, and another tenant's
# objects answer 404 (they do not exist for this caller).
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from .. import metering
from ..auth import Caller, hash_password, require_role, require_tenant_admin
from ..db import Flavor, Job, User, Vm, get_session
from ..infra import images as hub_images, labhub
from ..schemas import (Accepted, FlavorModel, ImageOut, JobOut, NetworkOut, TenantOut, UsageReport, UserIn,
                       UserOut, VmIn, VmOut)
from ..services import (audit, check_quota, enqueue, gateway_of, hub_call, job_view, quota_of,
                        tenant_allocations, usage_of, vm_views)
from .. import tasks

PROV = "Provisioning & configuration"
BIZ = "Business support"
PORT = "Portability & interoperability"

router = APIRouter(prefix="/tenant", dependencies=[Depends(require_role("consumer"))])
consumer = require_role("consumer")


def tenant_out(s, t):
    return TenantOut(name=t.name, display_name=t.display_name, vni=t.vni, subnet=t.subnet,
                     gateway=gateway_of(t.subnet), quota=quota_of(t), usage=usage_of(s, t), created=t.created)


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
    except HTTPException as e:
        audit(s, c, "vm.create", body.name, "denied", e.detail)
        raise
    rec = Vm(name=body.name, tenant_id=t.id, owner=c.username, flavor=f.name, image=body.image)
    s.add(rec); s.commit()                      # counts against the quota from now on
    try:
        j = enqueue(s, c, "vm.create", body.name, tasks.create_vm, tenant_id=t.id,
                    name=body.name, tenant=t.name, flavor=f.name, image=body.image, ip=body.ip)
    except HTTPException:
        s.delete(rec); s.commit()               # nothing was queued: release name and quota
        raise
    rec.job_id = j.id; s.add(rec); s.commit()
    audit(s, c, "vm.create", body.name, "accepted", f"flavor {f.name}, image {body.image}, job {j.id}")
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


@router.get("/jobs", response_model=list[JobOut], tags=[PROV], summary="My jobs, newest first")
def jobs(limit: int = 50, c: Caller = Depends(consumer), s: Session = Depends(get_session)):
    rows = s.exec(select(Job).where(Job.tenant_id == c.tenant.id).order_by(Job.created.desc()).limit(min(limit, 500)))
    return [job_view(s, j) for j in rows]


@router.get("/jobs/{job_id}", response_model=JobOut, tags=[PROV], summary="One of my jobs")
def job(j: Job = Depends(my_job), s: Session = Depends(get_session)):
    return job_view(s, j)
