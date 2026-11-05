# db.py - the API's own records: who, which tenant, how much, what happened.
# Infrastructure facts (nodes, VNIs in use, addresses) stay in the hub's registries.
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine, select

from . import config

engine = create_engine(config.DB_URL, connect_args={"check_same_thread": False}
                       if config.DB_URL.startswith("sqlite") else {})


def now():
    return datetime.now(timezone.utc)


def utc(dt):
    """query parameters may come without a zone: read them as UTC"""
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Tenant(SQLModel, table=True):
    """CCRA business support: a customer, its contract (quota) and its network on the hub"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)          # = hub tenant name
    display_name: str = ""
    vni: int = Field(unique=True)
    subnet: str
    max_vms: int
    max_vcpus: int
    max_mem_mb: int
    max_disk_gb: int
    max_tunnels: int = 2
    created: datetime = Field(default_factory=now)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    role: str                                            # provider | consumer | auditor
    tenant_id: Optional[int] = Field(default=None, foreign_key="tenant.id")
    tenant_admin: bool = False                           # consumer who may add users to its tenant
    created: datetime = Field(default_factory=now)


class Flavor(SQLModel, table=True):
    """CCRA pricing/rating: what a tenant may order, and what an hour of it costs"""
    name: str = Field(primary_key=True)
    vcpus: int
    mem_mb: int
    disk_gb: int
    price_per_hour: float = 0.0


class Vm(SQLModel, table=True):
    """the API's view of a tenant VM: owner and flavor (the hub knows node, address, MAC)"""
    name: str = Field(primary_key=True)                  # = hub guest name, unique cluster-wide
    tenant_id: int = Field(foreign_key="tenant.id", index=True)
    owner: str
    flavor: str
    image: str = "cirros"
    status: str = "creating"                             # creating | active | error | deleting
    error: str = ""
    job_id: str = ""
    created: datetime = Field(default_factory=now)


class Job(SQLModel, table=True):
    """the job queue: the API adds rows, the worker (python -m app.worker) runs them in order"""
    id: str = Field(primary_key=True)
    kind: str                                            # vm.create, image.add, node.add, ...
    target: str = ""
    tenant_id: Optional[int] = Field(default=None, index=True)
    username: str
    created: datetime = Field(default_factory=now, index=True)
    func: str = ""                                       # a name from tasks.TASKS
    kwargs: str = "{}"                                   # JSON
    state: str = Field(default="queued", index=True)     # queued | started | finished | failed
    started: Optional[datetime] = None
    ended: Optional[datetime] = None
    result: str = ""                                     # JSON
    error: str = ""


class WorkerBeat(SQLModel, table=True):
    """the worker's heartbeat, so /provider/health can tell whether jobs will run"""
    id: int = Field(default=1, primary_key=True)
    ts: datetime = Field(default_factory=now)
    pid: int = 0


class AuditEvent(SQLModel, table=True):
    """CCRA reporting and auditing: every change and every refused attempt"""
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=now, index=True)
    username: str
    role: str
    tenant: str = ""                                     # tenant the action concerned
    action: str                                          # e.g. vm.create
    target: str = ""
    result: str                                          # ok | accepted | denied | error
    detail: str = ""


class UsageSample(SQLModel, table=True):
    """CCRA metering: one row per running VM per sampling interval"""
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=now, index=True)
    tenant: str = Field(index=True)
    vm: str
    flavor: str = ""
    vcpus: int
    mem_mb: int
    minutes: int
    cost: float = 0.0


class ConsoleSession(SQLModel, table=True):
    """a web console (ttyd on the hub) opened through the API; its id is the URL secret"""
    id: str = Field(primary_key=True)                    # 32 hex: /console/<id>/
    vm: str
    tenant_id: int = Field(index=True)
    username: str
    port: int
    cred: str                                            # ttyd basic auth, added by the proxy
    created: datetime = Field(default_factory=now)
    expires: datetime
    used: bool = False
    closed: bool = False


class Tunnel(SQLModel, table=True):
    """a Cloudflare quick tunnel (cloudflared on this PC) to a guest's port, or to a web console"""
    id: str = Field(primary_key=True)                    # 12 hex
    vm: str
    tenant_id: int = Field(index=True)
    username: str
    protocol: str                                        # http | https | ssh | tcp | console
    port: int = 0
    origin: str                                          # what cloudflared forwards to
    url: str = ""                                        # https://<random>.trycloudflare.com
    pid: int = 0
    console_id: str = ""                                 # for protocol console
    created: datetime = Field(default_factory=now)
    expires: datetime
    closed: bool = False


DEFAULT_FLAVORS = [
    Flavor(name="tiny", vcpus=1, mem_mb=256, disk_gb=2, price_per_hour=0.005),
    Flavor(name="small", vcpus=1, mem_mb=512, disk_gb=5, price_per_hour=0.01),
    Flavor(name="medium", vcpus=2, mem_mb=1024, disk_gb=10, price_per_hour=0.02),
]


def _add_missing_columns():
    """databases from an earlier version: add columns that tables have gained since"""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                ddl = col.type.compile(dialect=engine.dialect)
                lit = "" if default is None else (f" DEFAULT {int(default)}" if isinstance(default, bool) else
                                                  f" DEFAULT {default}" if isinstance(default, (int, float)) else
                                                  " DEFAULT '" + str(default).replace("'", "''") + "'")
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}{lit}'))
                if table.name == "job" and col.name == "state":
                    # jobs of the Redis era: their outcome was kept in Redis, not here
                    conn.execute(text("UPDATE job SET state = 'unknown'"))


def init_db():
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()
    with Session(engine) as s:
        if not s.exec(select(Flavor)).first():           # seed flavors once
            s.add_all([Flavor(**f.model_dump()) for f in DEFAULT_FLAVORS])
            s.commit()


def get_session():
    with Session(engine) as s:
        yield s
