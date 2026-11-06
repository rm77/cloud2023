# access.py - how tenants reach their guests: the internet switch, SSH keys (jump host and
# new guests), the jump host itself, and web consoles (ttyd on the hub, proxied by the API).
import secrets
from datetime import timedelta

from fastapi import HTTPException
from sqlmodel import Session, select

from . import config, tunnels
from .db import ConsoleSession, Tenant, Tunnel, Vm, now
from .infra import images as hub_images, labhub
from .schemas import AccessOut, ConsoleOut, GuestAccess, KeyOut, TunnelOut
from .services import audit, gateway_of, guest_ports, hub_call, refresh_edge, ssh_direct


def hub_tenants() -> dict:
    return {r["name"]: r for r in hub_call(labhub.hub_read, "tenants")}


def internet_of(row: dict | None) -> bool:
    return (row or {}).get("internet", "on") != "off"


def set_internet(t: Tenant, on: bool):
    hub_call(labhub.hub, "tenant", "set", t.name, "internet", "on" if on else "off")


def refresh_edge_routes():
    """the main PC's routes and forwards (staff access, campus ports); best effort"""
    refresh_edge()


# ---------------------------------------------------------------- SSH keys
def key_id(fp: str) -> str:
    return fp.removeprefix("SHA256:").replace("+", "-").replace("/", "_")


def list_keys(t: Tenant) -> list[KeyOut]:
    out = []
    for line in hub_call(labhub.hub, "keys", t.name).splitlines()[1:]:   # FINGERPRINT TYPE COMMENT
        f = line.split(None, 2)
        if len(f) >= 2:
            out.append(KeyOut(id=key_id(f[0]), fingerprint=f[0], type=f[1], comment=f[2] if len(f) > 2 else "-"))
    return out


def add_key(s: Session, caller, t: Tenant, key: str) -> KeyOut:
    fp = hub_call(labhub.hub, "key", "add", t.name, key.strip()).strip().splitlines()[-1]
    audit(s, caller, "key.add", fp, tenant=t.name)
    for k in list_keys(t):
        if k.fingerprint == fp:
            return k
    return KeyOut(id=key_id(fp), fingerprint=fp, type="?", comment="")


def delete_key(s: Session, caller, t: Tenant, kid: str):
    if not all(ch.isalnum() or ch in "-_" for ch in kid):
        raise HTTPException(422, "key id: the id from GET .../keys")
    hub_call(labhub.hub, "key", "del", t.name, kid)
    audit(s, caller, "key.delete", kid, tenant=t.name)


# ---------------------------------------------------------------- access summary
def access_of(s: Session, t: Tenant) -> AccessOut:
    row = hub_tenants().get(t.name)
    gw = gateway_of(t.subnet)
    host = config.EDGE_HOST or "<lab PC>"
    users = {i["name"]: i["user"] for i in hub_call(hub_images.list)}
    hub_vms = {r["name"]: r for r in hub_call(labhub.hub_read, "vms")}
    ports = guest_ports()
    jump = f"t-{t.name}@{host}:{config.JUMP_PORT}"
    guests = []
    for v in s.exec(select(Vm).where(Vm.tenant_id == t.id).order_by(Vm.name)):
        ip = hub_vms.get(v.name, {}).get("ip") or None
        user = users.get(v.image, "root")
        via_jump = f"ssh -J {jump} {user}@{ip}" if ip else ""
        port = ports.get(v.name)
        guests.append(GuestAccess(name=v.name, ip=ip, user=user, ssh_port=port,
                                  ssh=ssh_direct(port, user) or via_jump, ssh_jump=via_jump,
                                  console=f"POST /tenant/vms/{v.name}/console"))
    # ~/.ssh/config: the campus port where a guest has one, otherwise through the jump host
    cfg = ""
    for g in guests:
        if g.ssh_port:
            cfg += f"Host {t.name}-{g.name}\n  HostName {host}\n  Port {g.ssh_port}\n  User {g.user}\n"
        elif g.ip:
            cfg += f"Host {t.name}-{g.name}\n  HostName {g.ip}\n  User {g.user}\n  ProxyJump {jump}\n"
    return AccessOut(tenant=t.name, subnet=t.subnet, gateway=gw, dns=gw, internet=internet_of(row),
                     jump_host=host, jump_port=config.JUMP_PORT, jump_user=f"t-{t.name}",
                     keys=len(list_keys(t)), guests=guests, ssh_config=cfg)


# ---------------------------------------------------------------- web consoles
def _stop(cs: ConsoleSession):
    try:
        labhub.hub("console", "stop", cs.id, timeout=30)
    except labhub.LabError:
        pass


def expire_consoles(s: Session):
    for cs in s.exec(select(ConsoleSession).where(ConsoleSession.closed == False)):  # noqa: E712
        if cs.expires <= now():
            _stop(cs)
            cs.closed = True
            s.add(cs)
            _close_console_tunnels(s, cs.id)
    s.commit()


def console_public_url(s: Session, sid: str) -> str | None:
    t = s.exec(select(Tunnel).where(Tunnel.console_id == sid, Tunnel.closed == False)).first()  # noqa: E712
    return f"{t.url}/console/{sid}/" if t else None


def open_console(s: Session, caller, v: Vm, public: bool = False) -> ConsoleOut:
    expire_consoles(s)
    t = s.get(Tenant, v.tenant_id)
    if public:
        _tunnel_allowed(s, caller, t, v.name)
    sid, cred = secrets.token_hex(16), secrets.token_urlsafe(18)
    port = int(hub_call(labhub.hub, "console", "start", v.name, sid, cred).strip().splitlines()[-1])
    cs = ConsoleSession(id=sid, vm=v.name, tenant_id=v.tenant_id, username=caller.username, port=port, cred=cred,
                        expires=now() + timedelta(minutes=config.CONSOLE_MINUTES))
    s.add(cs); s.commit()
    public_url = None
    if public:
        # the tunnel reaches this API; requests through it may only use /console/ (main.py)
        try:
            tn = _start_tunnel(s, caller, v, t, "console", config.API_PORT, f"http://127.0.0.1:{config.API_PORT}",
                               cs.expires, console_id=sid)
        except HTTPException:
            _stop(cs); cs.closed = True; s.add(cs); s.commit()
            raise
        public_url = f"{tn.url}/console/{sid}/"
    audit(s, caller, "console.open", v.name, detail=f"session {sid[:8]}…{' public' if public else ''}",
          tenant=t.name if t else "")
    return console_out(s, cs, public_url)


def console_out(s: Session, cs: ConsoleSession, public_url: str | None = None) -> ConsoleOut:
    """the API's URL, the public one (tunnel), and ttyd itself on the campus address"""
    direct = direct_login = None
    if config.CONSOLE_PORTS_DIRECT:
        direct = f"http://{config.EDGE_HOST or '<lab PC>'}:{cs.port}/console/{cs.id}/"
        direct_login = f"lab / {cs.cred}"
    return ConsoleOut(id=cs.id, vm=cs.vm, url=f"/console/{cs.id}/",
                      public_url=public_url or console_public_url(s, cs.id),
                      direct_url=direct, direct_login=direct_login, expires=cs.expires)


def close_console(s: Session, caller, cs: ConsoleSession):
    _stop(cs)
    cs.closed = True
    s.add(cs); s.commit()
    _close_console_tunnels(s, cs.id)
    audit(s, caller, "console.close", cs.vm)


# ---------------------------------------------------------------- Cloudflare quick tunnels
DEFAULT_PORT = {"http": 80, "https": 443, "ssh": 22, "tcp": 0}


def _how(t: Tunnel, user: str) -> str:
    host = t.url.removeprefix("https://")
    if t.protocol in ("http", "https"):
        return f"open {t.url} in a browser, or: curl {t.url}"
    if t.protocol == "ssh":
        return f'ssh -o ProxyCommand="cloudflared access ssh --hostname %h" {user}@{host}'
    if t.protocol == "tcp":
        return (f"cloudflared access tcp --hostname {host} --url localhost:{t.port}   "
                f"(then connect to localhost:{t.port})")
    return f"open {t.url}/console/{t.console_id}/ in a browser"


def tunnel_out(s: Session, t: Tunnel) -> TunnelOut:
    v = s.get(Vm, t.vm)
    users = {}
    if t.protocol == "ssh":
        users = {i["name"]: i["user"] for i in hub_call(hub_images.list)}
    return TunnelOut(id=t.id, vm=t.vm, protocol=t.protocol, port=t.port, url=t.url, expires=t.expires,
                     how=_how(t, users.get(v.image if v else "", "root")))


def expire_tunnels(s: Session):
    """close tunnels that expired, or whose cloudflared is gone (crashed, PC rebooted)"""
    for t in s.exec(select(Tunnel).where(Tunnel.closed == False)):  # noqa: E712
        if t.expires <= now() or not tunnels.alive(t.pid):
            tunnels.stop(t.pid, t.id)
            t.closed = True
            s.add(t)
    s.commit()


def _close_console_tunnels(s: Session, sid: str):
    for t in s.exec(select(Tunnel).where(Tunnel.console_id == sid, Tunnel.closed == False)):  # noqa: E712
        tunnels.stop(t.pid, t.id)
        t.closed = True
        s.add(t)
    s.commit()


def _tunnel_allowed(s: Session, caller, t: Tenant, target: str):
    if not config.TUNNELS:
        raise HTTPException(403, "tunnels are switched off on this cluster")
    expire_tunnels(s)
    used = len(s.exec(select(Tunnel).where(Tunnel.tenant_id == t.id, Tunnel.closed == False)).all())  # noqa: E712
    limit = t.max_tunnels if t.max_tunnels is not None else 2
    if used >= limit:
        audit(s, caller, "tunnel.open", target, "denied", f"limit {limit} reached", tenant=t.name)
        raise HTTPException(403, f"tunnel limit reached: {used}/{limit} open (close one, or ask the provider)")


def _start_tunnel(s, caller, v: Vm, t: Tenant, protocol, port, origin, expires, console_id="") -> Tunnel:
    tid = secrets.token_hex(6)
    try:
        pid, url = tunnels.start(tid, origin, insecure_tls=(protocol == "https"))
    except tunnels.TunnelError as e:
        audit(s, caller, "tunnel.open", v.name, "error", str(e), tenant=t.name)
        raise HTTPException(502, f"tunnel: {e}")
    tn = Tunnel(id=tid, vm=v.name, tenant_id=t.id, username=caller.username, protocol=protocol, port=port,
                origin=origin, url=url, pid=pid, console_id=console_id, expires=expires)
    s.add(tn); s.commit(); s.refresh(tn)
    audit(s, caller, "tunnel.open", v.name, detail=f"{origin} -> {url}", tenant=t.name)
    return tn


def open_tunnel(s: Session, caller, v: Vm, protocol: str, port: int | None, hours: int | None) -> TunnelOut:
    t = s.get(Tenant, v.tenant_id)
    if v.status != "active":
        raise HTTPException(409, f"{v.name} is {v.status}")
    ip = {r["name"]: r for r in hub_call(labhub.hub_read, "vms")}.get(v.name, {}).get("ip")
    if not ip:
        raise HTTPException(409, f"{v.name} has no address yet")
    port = port or DEFAULT_PORT[protocol]
    if not port:
        raise HTTPException(422, "tcp tunnels need a port")
    _tunnel_allowed(s, caller, t, v.name)
    tn = _start_tunnel(s, caller, v, t, protocol, port, f"{protocol}://{ip}:{port}",
                       now() + timedelta(hours=hours or config.TUNNEL_HOURS))
    return tunnel_out(s, tn)


def close_tunnel(s: Session, caller, tn: Tunnel):
    tunnels.stop(tn.pid, tn.id)
    tn.closed = True
    s.add(tn); s.commit()
    t = s.get(Tenant, tn.tenant_id)
    audit(s, caller, "tunnel.close", tn.vm, detail=tn.url, tenant=t.name if t else "")
