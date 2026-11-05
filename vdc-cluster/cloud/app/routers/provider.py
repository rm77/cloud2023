# /provider - the CCRA Cloud Provider: onboarding, contracts, catalog, the pool of
# nodes, and a view across every tenant.
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session, select

from .. import config, metering, tasks
from ..auth import Caller, hash_password, require_role
from ..db import Flavor, Job, Tenant, User, Vm, WorkerBeat, get_session, now, utc
from ..infra import cluster as hub_cluster, images as hub_images, labhub, nodes as hub_nodes
from ..schemas import (Accepted, FlavorModel, ImageIn, ImageOut, JobOut, NodeOut, ProviderUserIn, Quota,
                       SpokeAdd, TenantIn, TenantOut, TenantUpdate, UsageReport, UserOut, VmOut)
from ..services import (audit, enqueue, hub_call, job_view, pick_network, vm_views)
from .tenant import tenant_out
from .. import access
from ..schemas import ConsoleIn, ConsoleOut, KeyIn, KeyOut, TunnelOut
from ..db import Tunnel

CUST = "Business support"
PROV = "Provisioning & configuration"
MON = "Monitoring & inventory"
PORT = "Portability & interoperability"
ACC = "Access & connectivity"

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
    hubmap = access.hub_tenants()
    return [tenant_out(s, t, hubmap) for t in s.exec(select(Tenant).order_by(Tenant.vni))]


@router.post("/tenants", response_model=TenantOut, status_code=201, tags=[CUST],
             summary="Onboard a tenant: network on the hub (VNI + subnet from the pool) and its admin user")
def onboard(body: TenantIn, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    if s.exec(select(Tenant).where(Tenant.name == body.name)).first():
        raise HTTPException(409, f"tenant {body.name} exists")
    if s.exec(select(User).where(User.username == body.admin_username)).first():
        raise HTTPException(409, f"user {body.admin_username} exists")
    if any(r["name"] == body.name for r in hub_call(labhub.hub_read, "tenants")):
        raise HTTPException(409, f"tenant {body.name} already exists on the hub (made with hubctl): "
                                 f"POST /provider/import adopts it")
    vni, subnet = pick_network(s, body.vni, body.subnet)
    hub_call(labhub.hub, "tenant", "add", body.name, vni, subnet)        # the network exists from here on
    q = body.quota or Quota(**config.DEFAULT_QUOTA)
    t = Tenant(name=body.name, display_name=body.display_name, vni=vni, subnet=subnet, **q.model_dump())
    s.add(t); s.commit(); s.refresh(t)
    s.add(User(username=body.admin_username, password_hash=hash_password(body.admin_password),
               role="consumer", tenant_id=t.id, tenant_admin=True))
    s.commit()
    if access.internet_of(access.hub_tenants().get(body.name)) != body.internet:
        access.set_internet(t, body.internet)
    access.refresh_edge_routes()
    audit(s, c, "tenant.create", body.name, detail=f"VNI {vni}, subnet {subnet}, admin {body.admin_username}, "
          f"internet {'on' if body.internet else 'off'}", tenant=body.name)
    return tenant_out(s, t)


def unmanaged_on_hub(s: Session):
    """tenants and VMs the hub has but the API does not know (made with hubctl directly)"""
    known_t = {t.name for t in s.exec(select(Tenant))}
    known_v = {v.name for v in s.exec(select(Vm))}
    tenants = [r for r in hub_call(labhub.hub_read, "tenants") if r["name"] not in known_t]
    vms = [r for r in hub_call(labhub.hub_read, "vms") if r["name"] not in known_v]
    return tenants, vms


@router.get("/unmanaged", tags=[CUST],
            summary="Tenants and VMs on the hub that the API does not know (created with hubctl)")
def unmanaged(s: Session = Depends(get_session)):
    tenants, vms = unmanaged_on_hub(s)
    return dict(tenants=[dict(name=r["name"], vni=int(r["vni"]), subnet=r["subnet"]) for r in tenants],
                vms=[dict(name=r["name"], tenant=r["tenant"], node=r["node"], ip=r["ip"],
                          mem_mb=int(r["mem"] or 0), cpus=int(r["cpus"] or 1)) for r in vms],
                hint="POST /provider/import adopts them" if tenants or vms else "nothing to import")


@router.post("/import", tags=[CUST],
             summary="Adopt tenants and VMs made with hubctl: default quota, no users (add them with POST /provider/users)")
def import_from_hub(c: Caller = Depends(provider), s: Session = Depends(get_session)):
    tenants, vms = unmanaged_on_hub(s)
    q = config.DEFAULT_QUOTA
    added_t, added_v, skipped = [], [], []
    for r in tenants:
        s.add(Tenant(name=r["name"], display_name="imported from hubctl", vni=int(r["vni"]),
                     subnet=r["subnet"], **q))
        added_t.append(r["name"])
    s.commit()
    tid = {t.name: t.id for t in s.exec(select(Tenant))}
    # a VM gets the flavor of exactly its size, else "custom" (counted as a VM, priced at 0)
    by_size = {(f.vcpus, f.mem_mb): f.name for f in s.exec(select(Flavor))}
    for r in vms:
        if r["tenant"] not in tid:
            skipped.append(f"{r['name']}: tenant {r['tenant']} unknown"); continue
        size = (int(r["cpus"] or 1), int(r["mem"] or 0))
        s.add(Vm(name=r["name"], tenant_id=tid[r["tenant"]], owner="hubctl", flavor=by_size.get(size, "custom"),
                 image=r["image"] or "cirros", status="active"))
        added_v.append(r["name"])
    s.commit()
    audit(s, c, "hub.import", ",".join(added_t + added_v)[:200],
          detail=f"{len(added_t)} tenant(s), {len(added_v)} VM(s)")
    return dict(tenants=added_t, vms=added_v, skipped=skipped,
                next="give each imported tenant an admin: POST /provider/users "
                     '{"role":"consumer","tenant":"<name>","tenant_admin":true,...}' if added_t else "")


@router.get("/tenants/{name}", response_model=TenantOut, tags=[CUST], summary="One tenant")
def tenant(name: str, s: Session = Depends(get_session)):
    return tenant_out(s, tenant_or_404(s, name))


@router.patch("/tenants/{name}", response_model=TenantOut, tags=[CUST], summary="Change a tenant's details")
def update_tenant(name: str, body: TenantUpdate, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    t = tenant_or_404(s, name)
    if body.display_name is not None:
        t.display_name = body.display_name
    if body.internet is not None:
        access.set_internet(t, body.internet)
    s.add(t); s.commit()
    audit(s, c, "tenant.update", name, detail=body.model_dump_json(exclude_none=True), tenant=name)
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
    access.refresh_edge_routes()
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


@router.get("/health", tags=[MON], summary="Is the hub reachable, how many nodes are Ready, is the job worker running")
def health(s: Session = Depends(get_session)):
    out = dict(hub="ok", nodes_ready=0, nodes_total=0, queue="ok")
    try:
        ns = hub_nodes.list()
        out["nodes_total"], out["nodes_ready"] = len(ns), sum(1 for n in ns if n["status"] == "Ready")
    except labhub.LabError as e:
        out["hub"] = f"unreachable: {e}"
    if config.QUEUE_BACKEND == "redis":
        try:
            from ..services import redis_conn
            redis_conn().ping()
        except Exception as e:                   # private Redis not running
            out["queue"] = f"Redis not reachable: {e.__class__.__name__} (./cluster.sh cloud status)"
    beat = s.get(WorkerBeat, 1)
    if out["queue"] == "ok" and (beat is None or (now() - utc(beat.ts)).total_seconds() > 30):
        out["queue"] = "worker not running: jobs wait (./cluster.sh cloud status)"
    out["queue_backend"] = config.QUEUE_BACKEND
    out["jobs_waiting"] = len(s.exec(select(Job).where(Job.state == "queued")).all())
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


# ---------------------------------------------------------------- access & connectivity
@router.get("/tenants/{name}/access", tags=[ACC], summary="A tenant's gateway, internet, jump host and guests")
def tenant_access(name: str, s: Session = Depends(get_session)):
    return access.access_of(s, tenant_or_404(s, name))


@router.get("/tenants/{name}/keys", response_model=list[KeyOut], tags=[ACC], summary="A tenant's SSH keys")
def tenant_keys(name: str, s: Session = Depends(get_session)):
    return access.list_keys(tenant_or_404(s, name))


@router.post("/tenants/{name}/keys", response_model=KeyOut, status_code=201, tags=[ACC],
             summary="Add an SSH key to a tenant")
def tenant_add_key(name: str, body: KeyIn, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    return access.add_key(s, c, tenant_or_404(s, name), body.key)


@router.delete("/tenants/{name}/keys/{key_id}", status_code=204, tags=[ACC], summary="Remove a tenant's SSH key")
def tenant_delete_key(name: str, key_id: str, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    access.delete_key(s, c, tenant_or_404(s, name), key_id)
    return Response(status_code=204)


@router.post("/vms/{name}/console", response_model=ConsoleOut, status_code=201, tags=[ACC],
             summary="Web console of any VM")
def any_console(name: str, body: ConsoleIn | None = None, c: Caller = Depends(provider),
                s: Session = Depends(get_session)):
    v = s.get(Vm, name)
    if v is None:
        raise HTTPException(404, f"no VM {name}")
    return access.open_console(s, c, v, public=bool(body and body.public))


@router.get("/tunnels", response_model=list[TunnelOut], tags=[ACC], summary="Every open Cloudflare tunnel")
def all_tunnels(s: Session = Depends(get_session)):
    access.expire_tunnels(s)
    return [access.tunnel_out(s, t) for t in s.exec(select(Tunnel).where(Tunnel.closed == False))]  # noqa: E712


@router.delete("/tunnels/{tid}", status_code=204, tags=[ACC], summary="Close any tunnel")
def kill_tunnel(tid: str, c: Caller = Depends(provider), s: Session = Depends(get_session)):
    t = s.get(Tunnel, tid)
    if t is None or t.closed:
        raise HTTPException(404, f"no open tunnel {tid}")
    access.close_tunnel(s, c, t)
    return Response(status_code=204)
