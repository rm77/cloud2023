# /provider - the CCRA Cloud Provider: onboarding, contracts, catalog, the pool of
# nodes, and a view across every tenant.
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session, select

from .. import config, metering, tasks
from ..auth import Caller, hash_password, require_role
from ..db import Flavor, Job, Tenant, User, Vm, get_session
from ..infra import cluster as hub_cluster, images as hub_images, labhub, nodes as hub_nodes
from ..schemas import (Accepted, FlavorModel, ImageIn, ImageOut, JobOut, NodeOut, ProviderUserIn, Quota,
                       SpokeAdd, TenantIn, TenantOut, TenantUpdate, UsageReport, UserOut, VmOut)
from ..services import (audit, enqueue, hub_call, job_view, pick_network, queue, vm_views)
from .tenant import tenant_out

CUST = "Business support"
PROV = "Provisioning & configuration"
MON = "Monitoring & inventory"
PORT = "Portability & interoperability"

provider = require_role("provider")
router = APIRouter(prefix="/provider", dependencies=[Depends(provider)])


def tenant_or_404(s, name) -> Tenant:
    t = s.exec(select(Tenant).where(Tenant.name == name)).first()
    if t is None:
        raise HTTPException(404, f"no tenant {name}")
    return t


# ---------------------------------------------------------------- customers and contracts
@router.get("/tenants", response_model=list[TenantOut], tags=[CUST], summary="All tenants")
def tenants(s: Session = Depends(get_session)):
    return [tenant_out(s, t) for t in s.exec(select(Tenant).order_by(Tenant.vni))]


@router.post("/tenants", response_model=TenantOut, status_code=201, tags=[CUST],
             summary="Onboard a tenant: network on the hub (VNI + subnet from the pool) and its admin user")
def onboard(body: TenantIn, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    if s.exec(select(Tenant).where(Tenant.name == body.name)).first():
        raise HTTPException(409, f"tenant {body.name} exists")
    if s.exec(select(User).where(User.username == body.admin_username)).first():
        raise HTTPException(409, f"user {body.admin_username} exists")
    vni, subnet = pick_network(s, body.vni, body.subnet)
    hub_call(labhub.hub, "tenant", "add", body.name, vni, subnet)        # the network exists from here on
    q = body.quota or Quota(**config.DEFAULT_QUOTA)
    t = Tenant(name=body.name, display_name=body.display_name, vni=vni, subnet=subnet, **q.model_dump())
    s.add(t); s.commit(); s.refresh(t)
    s.add(User(username=body.admin_username, password_hash=hash_password(body.admin_password),
               role="consumer", tenant_id=t.id, tenant_admin=True))
    s.commit()
    audit(s, c, "tenant.create", body.name, detail=f"VNI {vni}, subnet {subnet}, admin {body.admin_username}",
          tenant=body.name)
    return tenant_out(s, t)


@router.get("/tenants/{name}", response_model=TenantOut, tags=[CUST], summary="One tenant")
def tenant(name: str, s: Session = Depends(get_session)):
    return tenant_out(s, tenant_or_404(s, name))


@router.patch("/tenants/{name}", response_model=TenantOut, tags=[CUST], summary="Change a tenant's details")
def update_tenant(name: str, body: TenantUpdate, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    t = tenant_or_404(s, name)
    if body.display_name is not None:
        t.display_name = body.display_name
    s.add(t); s.commit()
    audit(s, c, "tenant.update", name, tenant=name)
    return tenant_out(s, t)


@router.put("/tenants/{name}/quota", response_model=TenantOut, tags=[CUST],
            summary="Contract management: set a tenant's quota")
def set_quota(name: str, q: Quota, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    t = tenant_or_404(s, name)
    for k, v in q.model_dump().items():
        setattr(t, k, v)
    s.add(t); s.commit()
    audit(s, c, "tenant.quota", name, detail=q.model_dump_json(), tenant=name)
    return tenant_out(s, t)


@router.delete("/tenants/{name}", status_code=204, tags=[CUST],
               summary="Off-board a tenant (refused while it still has VMs)")
def delete_tenant(name: str, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    t = tenant_or_404(s, name)
    if s.exec(select(Vm).where(Vm.tenant_id == t.id)).first():
        raise HTTPException(409, f"tenant {name} still has VMs: delete them first")
    hub_call(labhub.hub, "tenant", "del", name)
    for u in s.exec(select(User).where(User.tenant_id == t.id)):
        s.delete(u)
    s.delete(t); s.commit()
    audit(s, c, "tenant.delete", name, tenant=name)
    return Response(status_code=204)


@router.get("/users", response_model=list[UserOut], tags=[CUST], summary="All accounts")
def users(s: Session = Depends(get_session)):
    names = {t.id: t.name for t in s.exec(select(Tenant))}
    return [UserOut(username=u.username, role=u.role, tenant=names.get(u.tenant_id), tenant_admin=u.tenant_admin,
                    created=u.created) for u in s.exec(select(User).order_by(User.role, User.username))]


@router.post("/users", response_model=UserOut, status_code=201, tags=[CUST],
             summary="Create an account: another provider, an auditor, or a tenant user")
def add_user(body: ProviderUserIn, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    if s.exec(select(User).where(User.username == body.username)).first():
        raise HTTPException(409, f"user {body.username} exists")
    tid = None
    if body.role == "consumer":
        if not body.tenant:
            raise HTTPException(422, "a consumer needs a tenant")
        tid = tenant_or_404(s, body.tenant).id
    elif body.tenant:
        raise HTTPException(422, f"a {body.role} does not belong to a tenant")
    u = User(username=body.username, password_hash=hash_password(body.password), role=body.role,
             tenant_id=tid, tenant_admin=body.tenant_admin and body.role == "consumer")
    s.add(u); s.commit(); s.refresh(u)
    audit(s, c, "user.create", body.username, detail=body.role, tenant=body.tenant or "")
    return UserOut(username=u.username, role=u.role, tenant=body.tenant, tenant_admin=u.tenant_admin, created=u.created)


@router.delete("/users/{username}", status_code=204, tags=[CUST], summary="Delete an account")
def delete_user(username: str, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    u = s.exec(select(User).where(User.username == username)).first()
    if u is None:
        raise HTTPException(404, f"no user {username}")
    if u.username == c.username:
        raise HTTPException(409, "you cannot delete your own account")
    s.delete(u); s.commit()
    audit(s, c, "user.delete", username)
    return Response(status_code=204)


# ---------------------------------------------------------------- pricing / catalog
@router.get("/flavors", response_model=list[FlavorModel], tags=[CUST], summary="Flavors and their price")
def flavors(s: Session = Depends(get_session)):
    return s.exec(select(Flavor).order_by(Flavor.mem_mb)).all()


@router.put("/flavors/{name}", response_model=FlavorModel, tags=[CUST], summary="Create or change a flavor")
def put_flavor(name: str, body: FlavorModel, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    if body.name != name:
        raise HTTPException(422, "name in the URL and the body differ")
    f = s.get(Flavor, name) or Flavor(name=name, vcpus=1, mem_mb=64, disk_gb=1)
    for k, v in body.model_dump().items():
        setattr(f, k, v)
    s.add(f); s.commit(); s.refresh(f)
    audit(s, c, "flavor.put", name, detail=body.model_dump_json())
    return f


@router.delete("/flavors/{name}", status_code=204, tags=[CUST], summary="Delete a flavor no VM uses")
def delete_flavor(name: str, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    f = s.get(Flavor, name)
    if f is None:
        raise HTTPException(404, f"no flavor {name}")
    if s.exec(select(Vm).where(Vm.flavor == name)).first():
        raise HTTPException(409, f"flavor {name} is in use")
    s.delete(f); s.commit()
    audit(s, c, "flavor.delete", name)
    return Response(status_code=204)


@router.get("/images", response_model=list[ImageOut], tags=[PORT], summary="Image catalog")
def images():
    return hub_call(hub_images.list)


@router.post("/images", response_model=Accepted, status_code=202, tags=[PORT],
             summary="System portability: import a cloud image (the hub downloads it)")
def add_image(body: ImageIn, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    j = enqueue(s, c, "image.add", body.name, tasks.add_image, **body.model_dump(exclude_none=True))
    audit(s, c, "image.add", body.name, "accepted", body.url)
    return Accepted(job=j.id, status_url=f"/provider/jobs/{j.id}")


@router.delete("/images/{name}", status_code=204, tags=[PORT], summary="Remove an image from the catalog")
def delete_image(name: str, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    if s.exec(select(Vm).where(Vm.image == name)).first():
        raise HTTPException(409, f"image {name} is used by VMs")
    hub_call(hub_images.delete, name=name)
    audit(s, c, "image.delete", name)
    return Response(status_code=204)


# ---------------------------------------------------------------- resource pool
@router.get("/nodes", response_model=list[NodeOut], tags=[MON], summary="Cluster members: status, guests, free RAM")
def nodes():
    return hub_call(hub_nodes.list)


@router.post("/nodes", response_model=Accepted, status_code=202, tags=[PROV],
             summary="Rapid elasticity: clone spokes on the lab PC and join them")
def add_nodes(body: SpokeAdd, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    j = enqueue(s, c, "node.add", f"{body.count} spoke(s)", tasks.add_spokes, count=body.count)
    audit(s, c, "node.add", body.count, "accepted")
    return Accepted(job=j.id, status_url=f"/provider/jobs/{j.id}")


@router.delete("/nodes/{name}", status_code=204, tags=[PROV], summary="Evict a spoke from the hub")
def delete_node(name: str, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    hub_call(hub_nodes.delete, name=name)
    audit(s, c, "node.delete", name)
    return Response(status_code=204)


@router.get("/hosts", tags=[MON], summary="Physical resource layer: the VMs of this lab PC")
def hosts():
    return hub_call(hub_cluster.status)


@router.get("/inventory", tags=[MON], summary="Inventory: nodes, tenants and VMs in one document (NetBox feed)")
def inventory(s: Session = Depends(get_session)):
    recs = s.exec(select(Vm).order_by(Vm.name)).all()
    return dict(nodes=hub_call(hub_nodes.list),
                tenants=[dict(name=t.name, vni=t.vni, subnet=t.subnet) for t in s.exec(select(Tenant))],
                vms=[v.model_dump() for v in vm_views(s, recs, with_node=True)])


@router.get("/health", tags=[MON], summary="Is the hub reachable, how many nodes are Ready, is the queue up")
def health(s: Session = Depends(get_session)):
    out = dict(hub="ok", nodes_ready=0, nodes_total=0, queue="ok")
    try:
        ns = hub_nodes.list()
        out["nodes_total"], out["nodes_ready"] = len(ns), sum(1 for n in ns if n["status"] == "Ready")
    except labhub.LabError as e:
        out["hub"] = f"unreachable: {e}"
    try:
        queue().connection.ping()
    except Exception as e:                       # redis down or misconfigured
        out["queue"] = f"unreachable: {e}"
    return out


# ---------------------------------------------------------------- every tenant's VMs and jobs
@router.get("/vms", response_model=list[VmOut], tags=[PROV], summary="VMs of all tenants, with their node")
def all_vms(tenant: Optional[str] = None, state: bool = False, s: Session = Depends(get_session)):
    q = select(Vm).order_by(Vm.name)
    if tenant:
        q = q.where(Vm.tenant_id == tenant_or_404(s, tenant).id)
    return vm_views(s, s.exec(q).all(), with_node=True, with_state=state)


@router.delete("/vms/{name}", response_model=Accepted, status_code=202, tags=[PROV], summary="Delete any VM")
def delete_any_vm(name: str, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    v = s.get(Vm, name)
    if v is None:
        raise HTTPException(404, f"no VM {name}")
    v.status = "deleting"; s.add(v); s.commit()
    t = s.get(Tenant, v.tenant_id)
    j = enqueue(s, c, "vm.delete", name, tasks.delete_vm, tenant_id=v.tenant_id, name=name)
    audit(s, c, "vm.delete", name, "accepted", f"job {j.id}", tenant=t.name if t else "")
    return Accepted(job=j.id, status_url=f"/provider/jobs/{j.id}")


@router.get("/jobs", response_model=list[JobOut], tags=[PROV], summary="All jobs, newest first")
def jobs(limit: int = 50, s: Session = Depends(get_session)):
    return [job_view(s, j) for j in s.exec(select(Job).order_by(Job.created.desc()).limit(min(limit, 500)))]


@router.get("/jobs/{job_id}", response_model=JobOut, tags=[PROV], summary="One job")
def job(job_id: str, s: Session = Depends(get_session)):
    j = s.get(Job, job_id)
    if j is None:
        raise HTTPException(404, f"no job {job_id}")
    return job_view(s, j)


# ---------------------------------------------------------------- accounting
@router.get("/usage", response_model=UsageReport, tags=[CUST], summary="Accounting: metered usage and cost")
def usage(since: Optional[datetime] = None, tenant: Optional[str] = None, s: Session = Depends(get_session)):
    return metering.report(s, since=since, tenant=tenant)
