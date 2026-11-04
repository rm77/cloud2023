# /audit - the CCRA Cloud Auditor: read-only evidence. Two independent sources:
#   events   what the API was asked to do, by whom, and what it answered
#   journal  what the cluster actually ran (the kit's command journal on each node)
from collections import Counter
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlmodel import Session, select

from .. import metering
from ..auth import require_role
from ..db import AuditEvent, Tenant, Vm, get_session, utc
from ..infra import labhub
from ..schemas import EventOut, UsageReport
from ..services import hub_call, quota_of, usage_of, vm_views

TAG = "Auditor"
router = APIRouter(prefix="/audit", tags=[TAG], dependencies=[Depends(require_role("auditor", "provider"))])

JOURNAL_TOOLS = {"all", "ip", "bridge", "virsh", "qemu", "qemu-img", "nft", "files", "wget", "mkfs", "apk"}


@router.get("/events", response_model=list[EventOut], summary="API audit log, newest first")
def events(tenant: Optional[str] = None, username: Optional[str] = None, action: Optional[str] = None,
           result: Optional[str] = Query(None, pattern="^(ok|accepted|denied|error)$"),
           since: Optional[datetime] = None, limit: int = Query(200, le=5000), s: Session = Depends(get_session)):
    q = select(AuditEvent).order_by(AuditEvent.ts.desc(), AuditEvent.id.desc()).limit(limit)
    if tenant: q = q.where(AuditEvent.tenant == tenant)
    if username: q = q.where(AuditEvent.username == username)
    if action: q = q.where(AuditEvent.action == action)
    if result: q = q.where(AuditEvent.result == result)
    if since: q = q.where(AuditEvent.ts >= utc(since))
    return s.exec(q).all()


@router.get("/reports/{tenant}", summary="Everything about one tenant: contract, usage, VMs, activity, cost")
def report(tenant: str, since: Optional[datetime] = None, s: Session = Depends(get_session)):
    t = s.exec(select(Tenant).where(Tenant.name == tenant)).first()
    if t is None:
        raise HTTPException(404, f"no tenant {tenant}")
    ev = s.exec(select(AuditEvent).where(AuditEvent.tenant == tenant)).all()
    return dict(tenant=t.name, vni=t.vni, subnet=t.subnet, quota=quota_of(t), usage=usage_of(s, t),
                vms=vm_views(s, s.exec(select(Vm).where(Vm.tenant_id == t.id)).all(), with_node=True),
                activity=dict(Counter(f"{e.action}:{e.result}" for e in ev)),
                denied=[EventOut.model_validate(e, from_attributes=True) for e in ev if e.result == "denied"][-20:],
                metering=metering.report(s, since=since, tenant=tenant))


@router.get("/metering", response_model=UsageReport, summary="Metered usage of every tenant")
def meter(since: Optional[datetime] = None, s: Session = Depends(get_session)):
    return metering.report(s, since=since)


@router.get("/journal", response_class=PlainTextResponse,
            summary="What the cluster really ran: a node's command journal (/var/log/lab/commands)")
def journal(node: str = Query("hub", pattern=r"^[a-z][a-z0-9]{0,7}$"), tool: str = "all",
            lines: int = Query(200, ge=1, le=5000)):
    if tool not in JOURNAL_TOOLS:
        raise HTTPException(422, f"tool must be one of {sorted(JOURNAL_TOOLS)}")
    return hub_call(labhub.hub, "exec", node, "tail", "-n", lines, f"/var/log/lab/commands/{tool}.log")
