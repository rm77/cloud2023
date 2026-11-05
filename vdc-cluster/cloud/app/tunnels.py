# tunnels.py - Cloudflare quick tunnels. Each tunnel is one cloudflared process on this PC
# ("cloudflared tunnel --url <origin>"): Cloudflare gives it a random public address,
# https://<words>.trycloudflare.com, with no account and no DNS setup. This PC can reach every
# guest (it routes the tenant subnets via the hub), so the origin is the guest's own address.
# Quick tunnels are meant for testing: no uptime guarantee, and the address changes each time.
import os
import re
import signal
import subprocess
import time

from . import config

# the address cloudflared prints in a box ("|  https://<words>.trycloudflare.com  |"); not the
# api.trycloudflare.com it talks to, which also shows up in its error messages
URL_RE = re.compile(r"\|\s*(https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com)\s*\|")


class TunnelError(Exception):
    pass


def _paths(tid):
    d = config.TUNNEL_DIR
    return d, os.path.join(d, f"{tid}.log"), os.path.join(d, f"{tid}.pid")


def start(tid: str, origin: str, insecure_tls=False) -> tuple[int, str]:
    """start cloudflared for ORIGIN; returns (pid, public URL) once Cloudflare has assigned one"""
    if not config.TUNNELS:
        raise TunnelError("tunnels are switched off on this cluster (CLOUD_TUNNELS=no)")
    exe = config.CLOUDFLARED
    if not exe or not os.access(exe, os.X_OK):
        raise TunnelError("cloudflared is not installed: ./cluster.sh cloud install")
    d, log, pidf = _paths(tid)
    home = os.path.join(d, "home")                 # a private HOME: no ~/.cloudflared/config.yml
    os.makedirs(home, exist_ok=True)
    argv = [exe, "tunnel", "--no-autoupdate", "--protocol", config.TUNNEL_PROTOCOL, "--url", origin]
    if insecure_tls:
        argv.append("--no-tls-verify")
    with open(log, "w") as lf:
        p = subprocess.Popen(argv, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True, cwd=d, env=dict(os.environ, HOME=home))
    with open(pidf, "w") as f:
        f.write(str(p.pid))
    deadline = time.monotonic() + config.TUNNEL_WAIT
    while time.monotonic() < deadline:
        m = URL_RE.search(_read(log))
        if m:
            return p.pid, m.group(1)
        if p.poll() is not None:
            break
        time.sleep(0.3)
    stop(p.pid, tid)
    tail = " | ".join(_read(log).strip().splitlines()[-3:])
    raise TunnelError(f"cloudflared gave no public address ({tail or 'no output'})")


def stop(pid: int, tid: str):
    if pid and alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    for f in _paths(tid)[2:]:
        try:
            os.remove(f)
        except FileNotFoundError:
            pass


def alive(pid: int) -> bool:
    """still our cloudflared? (pids are reused)"""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ")
    except OSError:
        return False
    return b"tunnel" in cmd and b"--url" in cmd


def _read(path) -> str:
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return ""
