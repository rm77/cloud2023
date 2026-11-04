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
    id: str = Field(primary_key=True)
    kind: str                                            # vm.create, image.add, node.add, ...
    target: str = ""
    tenant_id: Optional[int] = Field(default=None, index=True)
    username: str
    created: datetime = Field(default_factory=now)


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


DEFAULT_FLAVORS = [
    Flavor(name="tiny", vcpus=1, mem_mb=256, disk_gb=2, price_per_hour=0.005),
    Flavor(name="small", vcpus=1, mem_mb=512, disk_gb=5, price_per_hour=0.01),
    Flavor(name="medium", vcpus=2, mem_mb=1024, disk_gb=10, price_per_hour=0.02),
]


def init_db():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        if not s.exec(select(Flavor)).first():           # seed flavors once
            s.add_all([Flavor(**f.model_dump()) for f in DEFAULT_FLAVORS])
            s.commit()


def get_session():
    with Session(engine) as s:
        yield s
