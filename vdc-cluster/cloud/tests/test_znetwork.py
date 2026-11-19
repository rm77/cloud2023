# Extra addresses, campus ports and floating IPs: ownership, quota, cleanup, audit.
# Runs after test_cloud.py (same database), with its own two tenants.
from tests.conftest import login, run_jobs

H = {}


def test_setup_two_tenants(client, hub):
    H["admin"] = login(client, "admin", "admin-pass-123")
    for t, u in (("tenant-n", "nina"), ("tenant-m", "max")):
        r = client.post("/provider/tenants", headers=H["admin"],
                        json=dict(name=t, admin_username=u, admin_password=u + "-pass-123"))
        assert r.status_code == 201, r.text
        H[u] = login(client, u, u + "-pass-123")
    q = client.get("/tenant/me", headers=H["nina"]).json()["quota"]
    assert (q["max_ips"], q["max_ports"], q["max_floating_ips"]) == (2, 5, 1)
    for name, who in (("n1", "nina"), ("m1", "max")):
        assert client.post("/tenant/vms", headers=H[who], json={"name": name}).status_code == 202
    run_jobs()


def test_vm_created_with_a_floating_ip(client, hub):
    r = client.post("/tenant/vms", headers=H["nina"], json={"name": "n2", "floating_ip": True})
    assert r.status_code == 202, r.text
    run_jobs()
    v = client.get("/tenant/vms/n2", headers=H["nina"]).json()
    assert v["status"] == "active" and v["floating_ip"] == "10.28.84.200", v
    assert hub.fips["10.28.84.200"] == ("tenant-n", "n2")
    g = next(g for g in client.get("/tenant/access", headers=H["nina"]).json()["guests"] if g["name"] == "n2")
    assert g["floating_ip"] == "10.28.84.200" and g["ssh"] == "ssh cirros@10.28.84.200"
    # the quota (1) is used now: a second VM with a floating IP is refused before anything is made
    r = client.post("/tenant/vms", headers=H["nina"], json={"name": "n3", "floating_ip": True})
    assert r.status_code == 403 and "floating ips 2/1" in r.json()["detail"]


def test_extra_addresses(client, hub):
    r = client.post("/tenant/ips", headers=H["nina"], json={"note": "metallb"})
    assert r.status_code == 201, r.text
    H["xip"] = r.json()["ip"]
    assert r.json()["note"] == "metallb" and H["xip"].startswith(client.get("/tenant/me", headers=H["nina"]).json()["subnet"].rsplit(".", 1)[0])
    vm_ip = client.get("/tenant/vms/n1", headers=H["nina"]).json()["ip"]
    assert client.post("/tenant/ips", headers=H["nina"], json={"ip": vm_ip}).status_code == 409   # a VM's
    assert client.post("/tenant/ips", headers=H["nina"]).status_code == 201                        # no body
    r = client.post("/tenant/ips", headers=H["nina"])
    assert r.status_code == 403 and "ips 3/2" in r.json()["detail"]                                 # quota 2
    alloc = client.get("/tenant/network", headers=H["nina"]).json()["allocations"]
    assert any(a["ip"] == H["xip"] and a["extra"] and a["vm"] is None for a in alloc)
    assert [i["ip"] for i in client.get("/tenant/ips", headers=H["max"]).json()] == []              # not max's
    assert client.delete(f"/tenant/ips/{H['xip']}", headers=H["max"]).status_code == 404
    assert client.get("/tenant/me", headers=H["nina"]).json()["usage"]["ips"] == 2


def test_a_new_vm_never_gets_an_extra_address(client, hub):
    extra = {i["ip"] for i in client.get("/tenant/ips", headers=H["nina"]).json()}
    assert client.post("/tenant/vms", headers=H["nina"], json={"name": "n5"}).status_code == 202
    run_jobs()
    assert client.get("/tenant/vms/n5", headers=H["nina"]).json()["ip"] not in extra
    assert client.delete("/tenant/vms/n5", headers=H["nina"]).status_code == 202
    run_jobs()


def test_campus_ports(client, hub):
    syncs = getattr(hub, "edge_syncs", 0)
    r = client.post("/tenant/ports", headers=H["nina"], json={"target": "n1", "port": 80})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["campus_port"] == 47000 and p["protocol"] == "tcp" and p["port"] == 80 and p["connect"].endswith(":47000")
    assert p["address"] == client.get("/tenant/vms/n1", headers=H["nina"]).json()["ip"]
    assert hub.edge_syncs > syncs                                   # the lab PC was told to forward it
    r = client.post("/tenant/ports", headers=H["nina"], json={"target": H["xip"], "port": 53, "protocol": "udp"})
    assert r.status_code == 201 and r.json()["protocol"] == "udp"
    # another tenant's VM is no target; bad input is refused before the hub sees it
    assert client.post("/tenant/ports", headers=H["nina"], json={"target": "m1", "port": 22}).status_code == 409
    assert client.post("/tenant/ports", headers=H["nina"], json={"target": "n1; rm", "port": 22}).status_code == 422
    assert client.post("/tenant/ports", headers=H["nina"], json={"target": "n1", "port": 0}).status_code == 422
    assert [x["campus_port"] for x in client.get("/tenant/ports", headers=H["nina"]).json()] == [47000, 47001]
    assert client.get("/tenant/ports", headers=H["max"]).json() == []
    assert client.delete("/tenant/ports/47000", headers=H["max"]).status_code == 404
    assert {x["tenant"] for x in client.get("/provider/ports", headers=H["admin"]).json()} == {"tenant-n"}


def test_floating_ips(client, hub):
    r = client.post("/tenant/floating-ips", headers=H["nina"], json={"target": H["xip"]})
    assert r.status_code == 403                                     # quota 1, n2 has one
    r = client.put("/provider/tenants/tenant-n/quota", headers=H["admin"], json=dict(
        max_vms=4, max_vcpus=4, max_mem_mb=2048, max_disk_gb=40, max_floating_ips=3))
    assert r.status_code == 200 and r.json()["quota"]["max_floating_ips"] == 3
    r = client.post("/tenant/floating-ips", headers=H["nina"], json={"target": H["xip"]})
    assert r.status_code == 201, r.text
    f = r.json()
    assert f["target"] == H["xip"] and f["address"] == H["xip"] and f["tenant"] == "tenant-n"
    H["fip"] = f["ip"]
    ips = {i["ip"]: i for i in client.get("/tenant/ips", headers=H["nina"]).json()}
    assert ips[H["xip"]]["floating_ip"] == H["fip"] and ips[H["xip"]]["ports"] == [47001]
    # a target already attached, and a target that is not mine: refused, and nothing stays booked
    before = len(hub.fips)
    assert client.post("/tenant/floating-ips", headers=H["nina"], json={"target": "n2"}).status_code == 409
    assert client.post("/tenant/floating-ips", headers=H["nina"], json={"target": "m1"}).status_code == 409
    assert len(hub.fips) == before
    # booked without a target, attached later; max cannot touch it
    r = client.post("/tenant/floating-ips", headers=H["nina"])
    assert r.status_code == 201 and r.json()["target"] is None
    spare = r.json()["ip"]
    assert client.post(f"/tenant/floating-ips/{spare}/associate", headers=H["max"], json={"target": "m1"}).status_code == 404
    r = client.post(f"/tenant/floating-ips/{spare}/associate", headers=H["nina"], json={"target": "n1"})
    assert r.status_code == 200 and r.json()["target"] == "n1"
    r = client.post(f"/tenant/floating-ips/{spare}/disassociate", headers=H["nina"])
    assert r.status_code == 200 and r.json()["target"] is None and hub.fips[spare] == ("tenant-n", "-")
    assert client.delete(f"/tenant/floating-ips/{spare}", headers=H["nina"]).status_code == 204
    assert spare not in hub.fips
    assert client.get("/tenant/me", headers=H["nina"]).json()["usage"]["floating_ips"] == 2
    # the provider and the carrier see the whole pool, free addresses too
    pool = client.get("/provider/floating-ips", headers=H["admin"]).json()
    assert len(pool) == 4 and any(p["tenant"] is None for p in pool)
    assert len(client.get("/carrier/floating-ips", headers=H["admin"]).json()) == 4
    assert [f["ip"] for f in client.get("/tenant/floating-ips", headers=H["max"]).json()] == []


def test_cleanup_follows_the_target(client, hub):
    # deleting a VM: its campus ports go, its floating IP is detached but stays booked
    assert client.delete("/tenant/vms/n1", headers=H["nina"]).status_code == 202
    run_jobs()
    assert [p["target"] for p in client.get("/tenant/ports", headers=H["nina"]).json()] == [H["xip"]]
    assert client.post("/tenant/vms", headers=H["nina"], json={"name": "n4"}).status_code == 202
    assert client.delete("/tenant/vms/n2", headers=H["nina"]).status_code == 202
    run_jobs()
    assert hub.fips["10.28.84.200"] == ("tenant-n", "-")
    # releasing an extra address: its port goes, its floating IP is detached
    assert client.delete(f"/tenant/ips/{H['xip']}", headers=H["nina"]).status_code == 204
    assert client.get("/tenant/ports", headers=H["nina"]).json() == []
    assert hub.fips[H["fip"]] == ("tenant-n", "-")


def test_provider_can_take_back_and_it_is_audited(client, hub):
    assert client.delete(f"/provider/floating-ips/{H['fip']}", headers=H["admin"]).status_code == 204
    assert H["fip"] not in hub.fips
    assert client.delete("/provider/floating-ips/10.9.9.9", headers=H["admin"]).status_code == 404
    acts = [e["action"] for e in client.get("/audit/events?limit=200", headers=H["admin"]).json() if e["tenant"] == "tenant-n"]
    for a in ("ip.add", "ip.delete", "port.add", "fip.allocate", "fip.associate", "fip.disassociate", "fip.release"):
        assert a in acts, a
    denied = [e for e in client.get("/audit/events?limit=200", headers=H["admin"]).json()
              if e["tenant"] == "tenant-n" and e["result"] == "denied"]
    assert any("quota exceeded" in e["detail"] for e in denied)
