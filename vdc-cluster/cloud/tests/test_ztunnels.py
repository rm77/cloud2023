# Cloudflare quick tunnels: a stand-in cloudflared (conftest.py) prints a trycloudflare URL.
import os

from sqlmodel import Session

from app import config, tunnels
from app.db import Tunnel, engine, now
from tests.conftest import login

H = {}


def test_http_tunnel_to_a_vm(client):
    H["zed"] = login(client, "zed", "zed-pass-123")
    H["admin"] = login(client, "admin", "admin-pass-123")
    r = client.post("/tenant/vms/z1/tunnels", headers=H["zed"], json={"protocol": "http", "port": 8080})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["url"].startswith("https://fake-") and t["url"].endswith(".trycloudflare.com") and t["port"] == 8080
    with Session(engine) as s:
        row = s.get(Tunnel, t["id"])
        assert tunnels.alive(row.pid) and row.origin.startswith("http://10.") and row.origin.endswith(":8080")
    H["t1"] = t


def test_ssh_tunnel_explains_the_client_side(client):
    t = client.post("/tenant/vms/z1/tunnels", headers=H["zed"], json={"protocol": "ssh"}).json()
    assert t["port"] == 22 and 'cloudflared access ssh --hostname %h" cirros@' in t["how"]
    H["t2"] = t
    assert client.get("/tenant/me", headers=H["zed"]).json()["usage"]["tunnels"] == 2


def test_limit_and_isolation(client):
    r = client.post("/tenant/vms/z1/tunnels", headers=H["zed"], json={"protocol": "http"})
    assert r.status_code == 403 and "limit" in r.json()["detail"]                    # 2 per tenant
    alice = login(client, "alice", "alice-pass-1")
    assert client.post("/tenant/vms/z1/tunnels", headers=alice, json={}).status_code == 404
    assert client.delete(f"/tenant/tunnels/{H['t1']['id']}", headers=alice).status_code == 404
    assert client.post("/tenant/vms/z1/tunnels", headers=H["zed"], json={"protocol": "tcp"}).status_code in (403, 422)
    assert {t["id"] for t in client.get("/provider/tunnels", headers=H["admin"]).json()} >= {H["t1"]["id"], H["t2"]["id"]}


def test_close_stops_cloudflared(client):
    with Session(engine) as s:
        pid = s.get(Tunnel, H["t1"]["id"]).pid
    assert client.delete(f"/tenant/tunnels/{H['t1']['id']}", headers=H["zed"]).status_code == 204
    import time
    for _ in range(20):
        if not tunnels.alive(pid):
            break
        time.sleep(0.1)
    assert not tunnels.alive(pid)
    assert [t["id"] for t in client.get("/tenant/tunnels", headers=H["zed"]).json()] == [H["t2"]["id"]]


def test_expired_or_dead_tunnels_are_closed(client):
    with Session(engine) as s:
        row = s.get(Tunnel, H["t2"]["id"]); row.expires = now(); s.add(row); s.commit(); pid = row.pid
    assert client.get("/tenant/tunnels", headers=H["zed"]).json() == []
    assert not os.path.exists(os.path.join(config.TUNNEL_DIR, f"{H['t2']['id']}.pid"))
    import time
    time.sleep(0.3)
    assert not tunnels.alive(pid)


def test_cloudflared_failure_is_reported(client):
    r = client.post("/tenant/vms/z1/tunnels", headers=H["zed"], json={"protocol": "http", "port": 9999})
    assert r.status_code == 502 and "no public address" in r.json()["detail"]
    assert client.get("/tenant/me", headers=H["zed"]).json()["usage"]["tunnels"] == 0


def test_public_console_through_the_tunnel(client, fake_ttyd):
    r = client.post("/tenant/vms/z1/console", headers=H["zed"], json={"public": True})
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["public_url"].startswith("https://fake-") and c["public_url"].endswith(f"/console/{c['id']}/")
    with Session(engine) as s:
        assert s.get(Tunnel, [t for t in s.exec(__import__("sqlmodel").select(Tunnel).where(
            Tunnel.console_id == c["id"]))][0].id).origin == f"http://127.0.0.1:{config.API_PORT}"
    via_cf = {"cf-ray": "8f00-SIN", "cf-connecting-ip": "203.0.113.9"}
    # through the tunnel: the console works, the rest of the API does not exist
    assert client.get(c["url"], headers=via_cf).status_code == 200
    assert client.get("/docs", headers=via_cf).status_code == 404
    assert client.post("/auth/token", headers=via_cf, data={"username": "admin", "password": "x"}).status_code == 404
    assert client.get("/docs").status_code == 200                                     # on the lab network: fine
    # closing the console closes its tunnel
    assert client.delete(f"/tenant/consoles/{c['id']}", headers=H["zed"]).status_code == 204
    assert client.get("/tenant/tunnels", headers=H["zed"]).json() == []


def test_tunnels_switched_off(client, monkeypatch):
    monkeypatch.setattr(config, "TUNNELS", False)
    r = client.post("/tenant/vms/z1/tunnels", headers=H["zed"], json={"protocol": "http"})
    assert r.status_code == 403 and "switched off" in r.json()["detail"]
