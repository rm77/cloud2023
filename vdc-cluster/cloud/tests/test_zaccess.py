# Access to guests: the internet switch, SSH keys, the jump host summary, web consoles.
# Runs after test_cloud.py (same database), with its own tenant.
from app.db import ConsoleSession, engine, now
from sqlmodel import Session
from tests.conftest import login, run_jobs

H = {}
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK0vT4rTestKeyForZed zed@laptop"


def test_onboard_with_internet_off(client, hub):
    H["admin"] = login(client, "admin", "admin-pass-123")
    r = client.post("/provider/tenants", headers=H["admin"], json=dict(
        name="tenant-z", admin_username="zed", admin_password="zed-pass-123", internet=False))
    assert r.status_code == 201, r.text
    assert r.json()["internet"] is False and r.json()["jump_user"] == "t-tenant-z"
    assert hub.internet["tenant-z"] == "off"
    H["zed"] = login(client, "zed", "zed-pass-123")
    assert client.get("/tenant/me", headers=H["zed"]).json()["internet"] is False


def test_provider_switches_internet_on(client, hub):
    r = client.patch("/provider/tenants/tenant-z", headers=H["admin"], json={"internet": True})
    assert r.status_code == 200 and r.json()["internet"] is True and hub.internet["tenant-z"] == "on"
    assert client.patch("/tenant/me", headers=H["zed"], json={"internet": False}).status_code == 405  # tenants can't


def test_keys(client, hub):
    r = client.post("/tenant/keys", headers=H["zed"], json={"key": KEY})
    assert r.status_code == 201, r.text
    kid = r.json()["id"]
    assert r.json()["fingerprint"].startswith("SHA256:") and r.json()["comment"] == "zed@laptop"
    assert client.post("/tenant/keys", headers=H["zed"], json={"key": KEY}).status_code == 409       # twice
    assert client.post("/tenant/keys", headers=H["zed"], json={"key": "ssh-ed25519 AAAA\nrm -rf /"}).status_code == 422
    assert [k["id"] for k in client.get("/tenant/keys", headers=H["zed"]).json()] == [kid]
    assert [k["id"] for k in client.get("/provider/tenants/tenant-z/keys", headers=H["admin"]).json()] == [kid]
    H["kid"] = kid


def test_vm_gets_access_summary(client, hub):
    assert client.post("/tenant/vms", headers=H["zed"], json={"name": "z1", "flavor": "tiny"}).status_code == 202
    run_jobs()
    a = client.get("/tenant/access", headers=H["zed"]).json()
    assert a["gateway"] == a["dns"] == "10.100.20.1".replace("100", str(client.get("/tenant/me", headers=H["zed"]).json()["vni"]))
    assert a["jump_user"] == "t-tenant-z" and a["jump_port"] == 45222 and a["keys"] == 1
    g = a["guests"][0]
    assert g["name"] == "z1" and g["user"] == "cirros" and g["ssh"].startswith("ssh -J t-tenant-z@") \
        and g["ssh"].endswith(f"cirros@{g['ip']}")
    assert "ProxyJump t-tenant-z@" in a["ssh_config"]


def test_web_console(client, hub, fake_ttyd):
    r = client.post("/tenant/vms/z1/console", headers=H["zed"])
    assert r.status_code == 201, r.text
    url = r.json()["url"]
    sid = r.json()["id"]
    page = client.get(url)                                   # no login: the URL is the secret
    assert page.status_code == 200 and "ttyd page of z1" in page.text
    assert client.get(url + "token").json() == {"token": "t0k"}
    with client.websocket_connect(url + "ws", subprotocols=["tty"]) as ws:
        assert ws.accepted_subprotocol == "tty"
        ws.send_bytes(b"0ls\r")
        assert ws.receive_bytes() == b"echo:0ls\r"
        ws.send_text('{"AuthToken":"t0k"}')
        assert ws.receive_text() == 'echo:{"AuthToken":"t0k"}'
    # one session: after the WebSocket closed, the URL is dead (the proxy marks it a moment later)
    import time
    for _ in range(30):
        if client.get(url).status_code == 404:
            break
        time.sleep(0.1)
    assert client.get(url).status_code == 404
    assert client.get("/console/" + "0" * 32 + "/").status_code == 404
    assert client.get("/console/../../etc/").status_code == 404
    assert sid not in [c["id"] for c in client.get("/tenant/consoles", headers=H["zed"]).json()]


def test_console_rules(client, hub, fake_ttyd):
    # another tenant's VM
    alice = login(client, "alice", "alice-pass-1")
    assert client.post("/tenant/vms/z1/console", headers=alice).status_code == 404
    # expiry
    r = client.post("/tenant/vms/z1/console", headers=H["zed"]).json()
    with Session(engine) as s:
        cs = s.get(ConsoleSession, r["id"]); cs.expires = now(); s.add(cs); s.commit()
    assert client.get(r["url"]).status_code == 404
    # the provider may open any VM's console; the tenant closes its own
    r = client.post("/provider/vms/z1/console", headers=H["admin"])
    assert r.status_code == 201
    r2 = client.post("/tenant/vms/z1/console", headers=H["zed"]).json()
    assert client.delete(f"/tenant/consoles/{r2['id']}", headers=H["zed"]).status_code == 204
    assert r2["id"] not in hub.consoles and client.get(r2["url"]).status_code == 404


def test_access_is_audited_and_keys_removed(client):
    tari = login(client, "tari", "tari-pass-1")
    acts = {e["action"] for e in client.get("/audit/events?tenant=tenant-z", headers=tari).json()}
    assert {"key.add", "console.open", "console.close", "tenant.update"} <= acts
    assert client.delete(f"/tenant/keys/{H['kid']}", headers=H["zed"]).status_code == 204
    assert client.get("/tenant/keys", headers=H["zed"]).json() == []
    assert client.delete("/tenant/keys/no;such", headers=H["zed"]).status_code == 422
