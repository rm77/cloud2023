# config.py - every setting comes from the environment (see README)
import os
import secrets
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # <kit>/cloud
KIT = Path(os.environ.get("VDC_KIT", BASE.parent))        # <kit>
STATE = KIT / "state" / "cloud"                           # database, secrets, venv (git-ignored)

DB_URL = os.environ.get("VDC_DB", f"sqlite:///{STATE / 'cloud.db'}")

# JWT signing key: set VDC_JWT_SECRET; otherwise one is generated once and kept in .jwt-secret
_secret_file = STATE / "jwt-secret"
JWT_SECRET = os.environ.get("VDC_JWT_SECRET")
if not JWT_SECRET:
    if not _secret_file.exists():
        STATE.mkdir(parents=True, exist_ok=True)
        _secret_file.write_text(secrets.token_hex(32))
        _secret_file.chmod(0o600)
    JWT_SECRET = _secret_file.read_text().strip()
TOKEN_MINUTES = int(os.environ.get("VDC_TOKEN_MINUTES", "480"))

# job queue: "redis" (RQ, the default; cluster.sh runs a private Redis on a socket in state/cloud/)
# or "sqlite" (the Job table itself, no Redis). Either way the Job table keeps each job's state.
QUEUE_BACKEND = os.environ.get("VDC_QUEUE_BACKEND", "redis")
REDIS_URL = os.environ.get("REDIS_URL", f"unix://{STATE / 'redis.sock'}?db=0")
QUEUE = os.environ.get("VDC_QUEUE", "vdc-cloud")

# first provider account, created at startup when no provider exists yet
ADMIN_USER = os.environ.get("VDC_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("VDC_ADMIN_PASSWORD", "")

# address pool for onboarding: VNI n -> SUBNET_TEMPLATE.format(vni=n), e.g. VNI 199 -> 10.199.20.0/24
VNI_MIN = int(os.environ.get("VDC_VNI_MIN", "100"))
VNI_MAX = int(os.environ.get("VDC_VNI_MAX", "254"))
SUBNET_TEMPLATE = os.environ.get("VDC_SUBNET_TEMPLATE", "10.{vni}.20.0/24")

# default quota of a new tenant
DEFAULT_QUOTA = dict(max_vms=4, max_vcpus=4, max_mem_mb=2048, max_disk_gb=40)

# metering: how often the sampler looks at running VMs (minutes)
METER_INTERVAL = int(os.environ.get("VDC_METER_MINUTES", "5"))

# access to guests (written by cluster.sh into state/cloud/cloud.env)
EDGE_HOST = os.environ.get("VDC_EDGE_HOST", "")            # the main PC's campus address
JUMP_PORT = int(os.environ.get("VDC_JUMP_PORT", "45222"))
GUEST_PASSWORD = os.environ.get("VDC_GUEST_PASSWORD", "vdclab")
CONSOLE_MINUTES = int(os.environ.get("VDC_WEBCONSOLE_MINUTES", "60"))

# Cloudflare quick tunnels (cloudflared, started by the API on this PC)
TUNNELS = os.environ.get("VDC_TUNNELS", "yes") == "yes"
CLOUDFLARED = os.environ.get("VDC_CLOUDFLARED", "")
TUNNEL_DIR = os.environ.get("VDC_TUNNEL_DIR", str(STATE / "tunnels"))
TUNNEL_HOURS = int(os.environ.get("VDC_TUNNEL_HOURS", "8"))
TUNNELS_PER_TENANT = int(os.environ.get("VDC_TUNNELS_PER_TENANT", "2"))
TUNNEL_PROTOCOL = os.environ.get("VDC_TUNNEL_PROTOCOL", "auto")
TUNNEL_WAIT = int(os.environ.get("VDC_TUNNEL_WAIT", "40"))        # seconds to wait for the public URL
API_PORT = int(os.environ.get("VDC_API_PORT", "36100"))
DEFAULT_QUOTA["max_tunnels"] = TUNNELS_PER_TENANT
