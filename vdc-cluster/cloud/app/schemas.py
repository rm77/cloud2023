# schemas.py - request and response bodies. The patterns repeat the kit's own rules
# (lib.sh), so a value that would break hubctl is refused with a 422 before any call.
from datetime import datetime
from ipaddress import IPv4Network
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

NAME = r"^[a-z][a-z0-9-]{0,15}$"          # tenant, guest, image (hub rule)
NODE = r"^[a-z][a-z0-9]{0,7}$"
USER = r"^[a-z][a-z0-9_.-]{1,31}$"


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    tenant: Optional[str] = None


class Quota(BaseModel):
    max_vms: int = Field(ge=0, le=100)
    max_vcpus: int = Field(ge=0, le=256)
    max_mem_mb: int = Field(ge=0, le=1_048_576)
    max_disk_gb: int = Field(ge=0, le=10_000)
    max_tunnels: int = Field(2, ge=0, le=20, description="Cloudflare tunnels open at the same time")


class Usage(BaseModel):
    vms: int
    vcpus: int
    mem_mb: int
    disk_gb: int
    tunnels: int = 0


# ---------------------------------------------------------------- tenants
class TenantIn(BaseModel):
    name: str = Field(pattern=NAME, examples=["tenant-a"])
    display_name: str = Field("", max_length=80)
    admin_username: str = Field(pattern=USER, examples=["alice"])
    admin_password: str = Field(min_length=8, max_length=128)
    vni: Optional[int] = Field(None, ge=1, le=65535, description="omit: next free VNI of the pool")
    internet: bool = Field(True, description="guests reach the internet through the hub (NAT)")
    subnet: Optional[IPv4Network] = Field(None, description="omit: from the pool template, e.g. 10.199.20.0/24")
    quota: Optional[Quota] = None

    @field_validator("subnet")
    @classmethod
    def subnet_size(cls, v):
        if v is not None and not 16 <= v.prefixlen <= 29:
            raise ValueError("subnet prefix must be /16 to /29")
        return v


class TenantOut(BaseModel):
    name: str
    display_name: str
    vni: int
    subnet: str
    gateway: str
    internet: bool = True
    jump_user: str = ""
    quota: Quota
    usage: Usage
    created: datetime


class TenantUpdate(BaseModel):
    display_name: Optional[str] = Field(None, max_length=80)
    internet: Optional[bool] = Field(None, description="switch the tenant's internet access on or off")


# ---------------------------------------------------------------- access to guests
class KeyIn(BaseModel):
    key: str = Field(max_length=4096, pattern=r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|"
                     r"sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/=]+( [^\r\n]*)?$",
                     examples=["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI... alice@laptop"])


class KeyOut(BaseModel):
    id: str                     # URL-safe fingerprint, for DELETE
    fingerprint: str            # SHA256:...
    type: str
    comment: str


class GuestAccess(BaseModel):
    name: str
    ip: Optional[str] = None
    user: str
    ssh: str
    console: str


class AccessOut(BaseModel):
    tenant: str
    subnet: str
    gateway: str
    dns: str
    internet: bool
    jump_host: str
    jump_port: int
    jump_user: str
    keys: int
    guests: list[GuestAccess]
    ssh_config: str


class ConsoleIn(BaseModel):
    public: bool = Field(False, description="also reachable from the internet, through a Cloudflare tunnel")


class ConsoleOut(BaseModel):
    id: str
    vm: str
    url: str
    public_url: Optional[str] = None
    expires: datetime
    note: str = "open the url in a browser; it works once, until it expires"


class TunnelIn(BaseModel):
    protocol: Literal["http", "https", "ssh", "tcp"] = "http"
    port: Optional[int] = Field(None, ge=1, le=65535, description="omit: 80 for http, 443 for https, 22 for ssh")
    hours: Optional[int] = Field(None, ge=1, le=72, description="omit: the provider's default (8)")


class TunnelOut(BaseModel):
    id: str
    vm: str
    protocol: str
    port: int
    url: str
    expires: datetime
    how: str


# ---------------------------------------------------------------- users
class UserIn(BaseModel):
    username: str = Field(pattern=USER)
    password: str = Field(min_length=8, max_length=128)


class ProviderUserIn(UserIn):
    role: Literal["provider", "auditor", "consumer"]
    tenant: Optional[str] = Field(None, pattern=NAME, description="required for role consumer")
    tenant_admin: bool = False


class UserOut(BaseModel):
    username: str
    role: str
    tenant: Optional[str] = None
    tenant_admin: bool = False
    created: datetime


# ---------------------------------------------------------------- catalog
class FlavorModel(BaseModel):
    name: str = Field(pattern=NAME)
    vcpus: int = Field(ge=1, le=64)
    mem_mb: int = Field(ge=64, le=262_144)
    disk_gb: int = Field(ge=1, le=2000)
    price_per_hour: float = Field(0.0, ge=0)


class ImageIn(BaseModel):
    name: str = Field(pattern=NAME, examples=["ubuntu"])
    url: str = Field(pattern=r"^https?://\S+$",
                     examples=["https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"])
    type: Literal["cloud-init", "cirros", "none"] = "cloud-init"
    mem: Optional[int] = Field(None, ge=64, description="minimum RAM (MB)")
    disk: Optional[str] = Field(None, pattern=r"^[0-9]{1,5}[MG]$")
    user: Optional[str] = Field(None, pattern=r"^[a-z_][a-z0-9_-]{0,31}$")


class ImageOut(BaseModel):
    name: str
    type: str
    mem_mb: int
    disk: str
    user: str
    size_mb: int


# ---------------------------------------------------------------- VMs
class VmIn(BaseModel):
    name: str = Field(pattern=NAME, examples=["a1"], description="unique in the whole cluster")
    flavor: str = Field("tiny", pattern=NAME)
    image: str = Field("cirros", pattern=NAME)
    ip: Optional[str] = Field(None, description="omit: next free address of the tenant subnet")


class VmOut(BaseModel):
    name: str
    tenant: str
    flavor: str
    image: str
    status: str                       # creating | active | error | deleting
    ip: Optional[str] = None
    mac: Optional[str] = None
    vcpus: int
    mem_mb: int
    node: Optional[str] = None        # provider and auditor only
    state: Optional[str] = None       # running | shut off | ... (when asked for)
    error: str = ""
    owner: str
    created: datetime


class Accepted(BaseModel):
    job: str
    status_url: str


class JobOut(BaseModel):
    id: str
    kind: str
    target: str
    tenant: Optional[str] = None
    username: str
    state: str                        # queued | started | finished | failed | unknown
    created: datetime
    ended: Optional[datetime] = None
    result: Optional[dict | list] = None
    error: Optional[str] = None


# ---------------------------------------------------------------- nodes, network
class NodeOut(BaseModel):
    name: str
    ip: str
    mac: str
    status: str
    guests: int
    ram_free_mb: Optional[int] = None
    ram_total_mb: Optional[int] = None


class SpokeAdd(BaseModel):
    count: int = Field(1, ge=1, le=20)


class Allocation(BaseModel):
    ip: str
    mac: Optional[str] = None
    vm: Optional[str] = None
    next_free: bool = False


class NetworkOut(BaseModel):
    tenant: str
    vni: int
    subnet: str
    gateway: str
    allocations: list[Allocation]


# ---------------------------------------------------------------- audit, metering
class EventOut(BaseModel):
    ts: datetime
    username: str
    role: str
    tenant: str
    action: str
    target: str
    result: str
    detail: str


class UsageLine(BaseModel):
    tenant: str
    vm: str
    flavor: str
    vm_hours: float
    vcpu_hours: float
    ram_gb_hours: float
    cost: float


class UsageReport(BaseModel):
    since: datetime
    until: datetime
    lines: list[UsageLine]
    total_cost: float
