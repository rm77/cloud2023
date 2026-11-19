# network.py - a tenant's extra addresses, campus ports and floating IPs.
# The hub keeps them (hubctl ip / port / fip; registries extips, tports, fips) and the main PC
# forwards them (cluster.sh edge sync). Here: ownership, quota, and the audit trail.
from fastapi import HTTPException
from sqlmodel import Session

from . import config
from .db import Tenant
from .infra import labhub
from .schemas import ExtraIpOut, FloatingIpOut, PortOut
from .services import audit, hub_call, network_counts, quota_of, refresh_edge


def _last_line(out: str) -> str:
    lines = out.strip().splitlines()
    return lines[-1].strip() if lines else ""


def _quota(s: Session, caller, t: Tenant, key: str, action: str, target: str):
    have = network_counts().get(t.name, {}).get(key, 0)
    limit = getattr(quota_of(t), "max_" + key)
    if have + 1 > limit:
        msg = f"quota exceeded: {key.replace('_', ' ')} {have + 1}/{limit}"
        audit(s, caller, action, target, "denied", msg, tenant=t.name)
        raise HTTPException(403, msg)


# ---------------------------------------------------------------- lists (resolved by the hub)
def list_fips(tenant: str | None = None, free: bool = False) -> list[FloatingIpOut]:
    out = []
    for r in hub_call(lambda: labhub.table(labhub.hub("fips", *([tenant] if tenant else [])))):
        t = r.get("tenant", "-")
        if t == "-" and not free:
            continue
        none = lambda v: None if v in (None, "", "-") else v  # noqa: E731
        out.append(FloatingIpOut(ip=r["floating"], tenant=none(t), target=none(r.get("target")),
                                 address=none(r.get("address"))))
    return out


def list_ports(tenant: str | None = None) -> list[PortOut]:
    host = config.EDGE_HOST or "<lab PC>"
    out = []
    for r in hub_call(lambda: labhub.table(labhub.hub("port", "list", *([tenant] if tenant else [])))):
        out.append(PortOut(campus_port=int(r["port"]), protocol=r["proto"], tenant=r["tenant"], target=r["target"],
                           address=None if r.get("address") in (None, "-") else r["address"], port=int(r["to"]),
                           connect=f"{host}:{r['port']}"))
    return out


def list_ips(tenant: str | None = None) -> list[ExtraIpOut]:
    rows = [r for r in hub_call(labhub.hub_read, "extips") if tenant is None or r["tenant"] == tenant]
    if not rows:
        return []
    fips = {(f.tenant, f.address): f.ip for f in list_fips(tenant)}
    ports = {}
    for p in list_ports(tenant):
        ports.setdefault((p.tenant, p.address), []).append(p.campus_port)
    return [ExtraIpOut(ip=r["ip"], tenant=r["tenant"], note=None if r["note"] in ("", "-") else r["note"],
                       floating_ip=fips.get((r["tenant"], r["ip"])), ports=ports.get((r["tenant"], r["ip"]), []))
            for r in rows]


def owner_of(registry: str, key: str, value: str) -> str | None:
    for r in hub_call(labhub.hub_read, registry):
        if r[key] == value:
            return r["tenant"]
    return None


def _mine(t: Tenant | None, registry: str, key: str, value: str, what: str):
    """404 unless VALUE exists (and, for a tenant caller, is that tenant's)"""
    owner = owner_of(registry, key, value)
    if owner is None or (t is not None and owner != t.name):
        raise HTTPException(404, f"no {what} {value}" + (f" in tenant {t.name}" if t is not None else ""))
    return owner


# ---------------------------------------------------------------- extra addresses
def add_ip(s: Session, caller, t: Tenant, ip: str | None, note: str | None) -> ExtraIpOut:
    _quota(s, caller, t, "ips", "ip.add", ip or "")
    args = ["ip", "add", t.name] + ([ip] if ip else []) + (["--note", note] if note else [])
    addr = _last_line(hub_call(labhub.hub, *args))
    audit(s, caller, "ip.add", addr, detail=note or "", tenant=t.name)
    return ExtraIpOut(ip=addr, tenant=t.name, note=note)


def delete_ip(s: Session, caller, t: Tenant | None, ip: str):
    owner = _mine(t, "extips", "ip", ip, "extra address")
    hub_call(labhub.hub, "ip", "del", ip)
    refresh_edge()                          # its ports and floating IP are gone with it
    audit(s, caller, "ip.delete", ip, tenant=owner)


# ---------------------------------------------------------------- campus ports
def add_port(s: Session, caller, t: Tenant, target: str, port: int, protocol: str,
             campus_port: int | None) -> PortOut:
    _quota(s, caller, t, "ports", "port.add", f"{target}:{port}/{protocol}")
    args = ["port", "add", t.name, target, port, "--proto", protocol]
    if campus_port:
        args += ["--at", campus_port]
    cp = int(_last_line(hub_call(labhub.hub, *args)))
    refresh_edge()
    audit(s, caller, "port.add", f"{cp}/{protocol}", f"-> {target}:{port}", tenant=t.name)
    for p in list_ports(t.name):
        if p.campus_port == cp:
            return p
    return PortOut(campus_port=cp, protocol=protocol, tenant=t.name, target=target, port=port,
                   connect=f"{config.EDGE_HOST or '<lab PC>'}:{cp}")


def delete_port(s: Session, caller, t: Tenant | None, campus_port: int):
    owner = _mine(t, "tports", "port", str(campus_port), "campus port")
    hub_call(labhub.hub, "port", "del", campus_port)
    refresh_edge()
    audit(s, caller, "port.delete", str(campus_port), tenant=owner)


# ---------------------------------------------------------------- floating IPs
def _get_fip(tenant: str, ip: str) -> FloatingIpOut:
    for f in list_fips(tenant):
        if f.ip == ip:
            return f
    return FloatingIpOut(ip=ip, tenant=tenant)


def alloc_fip(s: Session, caller, t: Tenant, target: str | None) -> FloatingIpOut:
    _quota(s, caller, t, "floating_ips", "fip.allocate", target or "")
    fip = _last_line(hub_call(labhub.hub, "fip", "alloc", t.name))
    audit(s, caller, "fip.allocate", fip, tenant=t.name)
    if target:
        try:
            hub_call(labhub.hub, "fip", "assoc", fip, target)
        except HTTPException:
            hub_call(labhub.hub, "fip", "release", fip)     # nothing half-done
            audit(s, caller, "fip.release", fip, detail=f"attaching to {target} failed", tenant=t.name)
            raise
        refresh_edge()
        audit(s, caller, "fip.associate", fip, f"-> {target}", tenant=t.name)
    return _get_fip(t.name, fip)


def associate_fip(s: Session, caller, t: Tenant, fip: str, target: str) -> FloatingIpOut:
    _mine(t, "fips", "fip", fip, "floating IP")
    hub_call(labhub.hub, "fip", "assoc", fip, target)
    refresh_edge()
    audit(s, caller, "fip.associate", fip, f"-> {target}", tenant=t.name)
    return _get_fip(t.name, fip)


def disassociate_fip(s: Session, caller, t: Tenant | None, fip: str) -> FloatingIpOut:
    owner = _mine(t, "fips", "fip", fip, "floating IP")
    hub_call(labhub.hub, "fip", "dissoc", fip)
    refresh_edge()
    audit(s, caller, "fip.disassociate", fip, tenant=owner)
    return _get_fip(owner, fip)


def release_fip(s: Session, caller, t: Tenant | None, fip: str):
    owner = _mine(t, "fips", "fip", fip, "floating IP")
    hub_call(labhub.hub, "fip", "release", fip)
    refresh_edge()
    audit(s, caller, "fip.release", fip, tenant=owner)
