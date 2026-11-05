# main.py - vdc-cloud: the cloud API of vdc-cluster, organised by the NIST Cloud Computing
# Reference Architecture (SP 500-292). Run:  uvicorn app.main:app --host 0.0.0.0 --port 36100
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from .auth import bootstrap_admin
from .db import engine, init_db
from .infra.labhub import LabError
from .routers import audit, auth, carrier, console, provider, tenant

log = logging.getLogger("vdc-cloud")

TAGS = [
    {"name": "Security: authentication", "description": "CCRA Security, every actor. Log in with "
     "`POST /auth/token`, then send `Authorization: Bearer <token>`. The role in the token decides "
     "which part of the API you may use."},
    {"name": "Business support", "description": "CCRA Cloud Service Management → Business Support: "
     "customer management (tenants, users), contract management (quotas), pricing and rating (flavors), "
     "accounting (usage)."},
    {"name": "Provisioning & configuration", "description": "CCRA Cloud Service Management → "
     "Provisioning/Configuration: rapid provisioning and resource changing (VMs, spokes), jobs."},
    {"name": "Portability & interoperability", "description": "CCRA Cloud Service Management → "
     "Portability/Interoperability: standard cloud images (qcow2, cloud-init NoCloud); this OpenAPI contract."},
    {"name": "Access & connectivity", "description": "How consumers reach their guests. CCRA Security "
     "(SSH keys, the jump host) and Cloud Carrier (the path from campus into the lab), plus the "
     "web console, and Cloudflare quick tunnels that publish a guest's port (or a web console) on "
     "the internet. The internet switch per tenant is on PATCH /provider/tenants/{name}."},
    {"name": "Monitoring & inventory", "description": "CCRA Provisioning → monitoring and reporting; "
     "Business Support → inventory. Resource pool and physical layer."},
    {"name": "Auditor", "description": "CCRA Cloud Auditor: read-only evidence. API audit log, per-tenant "
     "reports, metering, and the cluster's own command journal."},
    {"name": "Carrier", "description": "CCRA Cloud Carrier: the underlay transport between the lab PCs, hub and spokes."},
]

DESCRIPTION = """
Cloud API of the **vdc-cluster** teaching lab, organised by the NIST Cloud Computing Reference
Architecture (SP 500-292).

| Prefix | CCRA actor | Role |
|---|---|---|
| `/tenant` | Cloud Consumer | `consumer`: only its own tenant |
| `/provider` | Cloud Provider | `provider`: every tenant, the pool, the catalog |
| `/audit` | Cloud Auditor | `auditor` (or provider): read only |
| `/carrier` | Cloud Carrier | `provider`, `auditor`: the underlay |

Long operations answer `202 Accepted` with a job; follow its `status_url`.
"""


@asynccontextmanager
async def lifespan(app):
    init_db()
    with Session(engine) as s:
        msg = bootstrap_admin(s)
        if msg:
            log.warning(msg)
        from . import access
        access.expire_tunnels(s)             # tunnels that died while the API was down
    yield


class TunnelGuard:
    """requests that come in through a Cloudflare tunnel may only use /console/ (public web
    consoles); everything else of the API stays on the lab network"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            h = {k.decode().lower(): v.decode(errors="replace") for k, v in scope.get("headers", [])}
            via_cf = "cf-ray" in h or "cf-connecting-ip" in h or h.get("host", "").endswith(".trycloudflare.com")
            if via_cf and not scope.get("path", "").startswith("/console/"):
                if scope["type"] == "http":
                    await send({"type": "http.response.start", "status": 404,
                                "headers": [(b"content-type", b"text/plain")]})
                    await send({"type": "http.response.body", "body": b"not found"})
                else:
                    await send({"type": "websocket.close", "code": 4404})
                return
        await self.app(scope, receive, send)


app = FastAPI(title="vdc-cloud", version="1.0", description=DESCRIPTION, openapi_tags=TAGS, lifespan=lifespan)
for r in (auth.router, tenant.router, provider.router, audit.router, carrier.router, console.router):
    app.include_router(r)
app.add_middleware(TunnelGuard)


@app.exception_handler(LabError)
async def cluster_error(request: Request, exc: LabError):
    """a cluster failure not handled by a route: the hub said no, or could not be reached"""
    return JSONResponse(status_code=502, content={"detail": f"cluster: {exc}"})


@app.get("/", include_in_schema=False)
def root():
    return {"service": "vdc-cloud", "docs": "/docs", "openapi": "/openapi.json"}
