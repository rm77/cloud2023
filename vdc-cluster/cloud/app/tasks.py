# tasks.py - what the worker (python -m app.worker) runs. Each task updates the API's record
# when the cluster is done, so the portal shows "active" or "error" without anyone polling.
from sqlmodel import Session

from .db import Flavor, Vm, engine
from .infra import cluster as hub_cluster, images as hub_images, vms as hub_vms
from .infra.labhub import LabError


def create_vm(name, tenant, flavor, image, ip=None):
    with Session(engine) as s:
        f = s.get(Flavor, flavor)
        rec = s.get(Vm, name)
        try:
            out = hub_vms.create(tenant=tenant, name=name, image=image, ip=ip,
                                 mem=f.mem_mb, cpus=f.vcpus, disk=f"{f.disk_gb}G")
            if rec:
                rec.status, rec.error = "active", ""
            return out
        except LabError as e:
            if rec:
                rec.status, rec.error = "error", str(e)[:300]
            raise
        finally:
            if rec:
                s.add(rec); s.commit()


def delete_vm(name):
    with Session(engine) as s:
        rec = s.get(Vm, name)
        try:
            hub_vms.delete(name=name)
        except LabError as e:
            if "no guest" not in str(e):           # already gone on the hub: just drop the record
                if rec:
                    rec.status, rec.error = "error", f"delete failed: {e}"[:300]; s.add(rec); s.commit()
                raise
        if rec:
            s.delete(rec); s.commit()
        return dict(deleted=name)


def add_image(**kw):
    return hub_images.add(**kw)


def add_spokes(count):
    return hub_cluster.spoke_add(count=count)



TASKS = {f.__name__: f for f in (create_vm, delete_vm, add_image, add_spokes)}


def name_of(func) -> str:
    if TASKS.get(func.__name__) is not func:
        raise ValueError(f"{func} is not a task")
    return func.__name__
