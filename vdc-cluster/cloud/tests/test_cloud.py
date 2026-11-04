# One story, in order: the provider onboards two tenants, each tenant works in its own
# space, the access rules hold, quotas bite, jobs finish, the auditor sees all of it.
from app import metering
from tests.conftest import login, run_jobs

H = {}


def test_provider_logs_in(client):
    H["admin"] = login(client, "admin", "admin-pass-123")
    assert client.get("/auth/me", headers=H["admin"]).json()["role"] == "provider"


def test_wrong_password_is_refused_and_audited(client):
    assert client.post("/auth/token", data=dict(username="admin", password="nope")).status_code == 401


def test_onboard_tenant_a_with_given_network(client, hub):
    r = client.post("/provider/tenants", headers=H["admin"], json=dict(
        name="tenant-a", admin_username="alice", admin_password="alice-pass-1", vni=199, subnet="10.199.20.0/24"))
    assert r.status_code == 201, r.text
    assert r.json()["gateway"] == "10.199.20.1"
    assert "hubctl tenant add tenant-a 199 10.199.20.0/24" in hub.calls


def test_onboard_tenant_b_from_the_pool(client, hub):
    r = client.post("/provider/tenants", headers=H["admin"], json=dict(
        name="tenant-b", admin_username="bob", admin_password="bob-pass-12",
        quota=dict(max_vms=1, max_vcpus=1, max_mem_mb=512, max_disk_gb=5)))
    assert r.status_code == 201, r.text
    assert (r.json()["vni"], r.json()["subnet"]) == (100, "10.100.20.0/24")   # first free VNI of the pool


def test_onboarding_refuses_clashes(client):
    r = client.post("/provider/tenants", headers=H["admin"], json=dict(
        name="tenant-c", admin_username="carol", admin_password="carol-pass", vni=199))
    assert r.status_code == 409 and "VNI 199" in r.json()["detail"]
    r = client.post("/provider/tenants", headers=H["admin"], json=dict(
        name="tenant-c", admin_username="carol", admin_password="carol-pass", subnet="10.199.0.0/16"))
    assert r.status_code == 409 and "overlaps" in r.json()["detail"]
    r = client.post("/provider/tenants", headers=H["admin"], json=dict(
        name="Bad;Name", admin_username="carol", admin_password="carol-pass"))
    assert r.status_code == 422                                                  # refused before any hub call


def test_tenant_admins_log_in(client):
    H["alice"] = login(client, "alice", "alice-pass-1")
    H["bob"] = login(client, "bob", "bob-pass-12")
    me = client.get("/tenant/me", headers=H["alice"]).json()
    assert me["name"] == "tenant-a" and me["subnet"] == "10.199.20.0/24"


def test_roles_are_enforced(client):
    assert client.get("/provider/tenants", headers=H["alice"]).status_code == 403
    assert client.get("/audit/events", headers=H["bob"]).status_code == 403
    assert client.get("/tenant/me", headers=H["admin"]).status_code == 403   # provider is not a tenant
    assert client.get("/tenant/me").status_code == 401


def test_alice_creates_a_vm_async(client, hub):
    r = client.post("/tenant/vms", headers=H["alice"], json=dict(name="a1", flavor="small"))
    assert r.status_code == 202, r.text
    H["job_a1"] = r.json()["job"]
    assert client.get("/tenant/vms/a1", headers=H["alice"]).json()["status"] == "creating"
    run_jobs()
    j = client.get(r.json()["status_url"], headers=H["alice"]).json()
    assert j["state"] == "finished", j
    vm = client.get("/tenant/vms/a1", headers=H["alice"]).json()
    assert (vm["status"], vm["ip"], vm["mem_mb"], vm["state"]) == ("active", "10.199.20.10", 512, "running")
    assert vm["node"] is None                                                    # tenants do not see placement
    assert any("hubctl vm add tenant-a a1 --image cirros --mem 512 --cpus 1 --disk 5G" in c for c in hub.calls)


def test_bob_creates_a_vm_in_his_own_subnet(client):
    r = client.post("/tenant/vms", headers=H["bob"], json=dict(name="b1", flavor="small"))
    assert r.status_code == 202
    run_jobs()
    assert client.get("/tenant/vms/b1", headers=H["bob"]).json()["ip"] == "10.100.20.10"


def test_tenants_cannot_see_each_other(client):
    assert [v["name"] for v in client.get("/tenant/vms", headers=H["bob"]).json()] == ["b1"]
    assert client.get("/tenant/vms/a1", headers=H["bob"]).status_code == 404     # exists, but not for bob
    assert client.delete("/tenant/vms/a1", headers=H["bob"]).status_code == 404
    assert client.get(f"/tenant/jobs/{H['job_a1']}", headers=H["bob"]).status_code == 404
    assert all(a["vm"] in (None, "b1") for a in client.get("/tenant/network", headers=H["bob"]).json()["allocations"])


def test_names_are_unique_cluster_wide(client):
    r = client.post("/tenant/vms", headers=H["bob"], json=dict(name="a1", flavor="tiny"))
    assert r.status_code == 409


def test_quota_refuses_and_is_audited(client):
    r = client.post("/tenant/vms", headers=H["bob"], json=dict(name="b2", flavor="tiny"))
    assert r.status_code == 403 and "quota exceeded" in r.json()["detail"]
    r = client.post("/tenant/vms", headers=H["alice"], json=dict(name="a2", flavor="huge"))
    assert r.status_code == 404


def test_provider_raises_bobs_quota(client):
    r = client.put("/provider/tenants/tenant-b/quota", headers=H["admin"],
                   json=dict(max_vms=2, max_vcpus=2, max_mem_mb=1024, max_disk_gb=10))
    assert r.status_code == 200
    assert client.post("/tenant/vms", headers=H["bob"], json=dict(name="b2", flavor="tiny")).status_code == 202
    run_jobs()


def test_tenant_admin_adds_a_member(client):
    r = client.post("/tenant/users", headers=H["alice"], json=dict(username="andi", password="andi-pass-1"))
    assert r.status_code == 201
    H["andi"] = login(client, "andi", "andi-pass-1")
    assert client.post("/tenant/users", headers=H["andi"], json=dict(username="x1", password="x1-pass-12")).status_code == 403
    assert [v["name"] for v in client.get("/tenant/vms", headers=H["andi"]).json()] == ["a1"]


def test_provider_sees_everything_with_placement(client):
    vms = client.get("/provider/vms", headers=H["admin"]).json()
    assert {v["name"]: v["tenant"] for v in vms} == {"a1": "tenant-a", "b1": "tenant-b", "b2": "tenant-b"}
    assert all(v["node"] for v in vms)
    inv = client.get("/provider/inventory", headers=H["admin"]).json()
    assert len(inv["nodes"]) == 3 and len(inv["tenants"]) == 2
    assert client.get("/provider/health", headers=H["admin"]).json()["nodes_ready"] == 3


def test_delete_tenant_refused_while_it_has_vms(client):
    assert client.delete("/provider/tenants/tenant-b", headers=H["admin"]).status_code == 409


def test_metering_and_usage(client):
    assert metering.sample_once(minutes=60) == 3                                 # three running VMs, one hour
    u = client.get("/tenant/usage", headers=H["bob"]).json()
    assert {l["vm"] for l in u["lines"]} == {"b1", "b2"}
    assert abs(u["total_cost"] - (0.01 + 0.005)) < 1e-9
    assert len(client.get("/provider/usage", headers=H["admin"]).json()["lines"]) == 3


def test_auditor(client):
    r = client.post("/provider/users", headers=H["admin"], json=dict(username="tari", password="tari-pass-1", role="auditor"))
    assert r.status_code == 201
    H["tari"] = login(client, "tari", "tari-pass-1")
    ev = client.get("/audit/events?result=denied", headers=H["tari"]).json()
    assert {e["action"] for e in ev} >= {"auth.login", "vm.create"}               # bad password, bob over quota
    rep = client.get("/audit/reports/tenant-b", headers=H["tari"]).json()
    assert rep["usage"]["vms"] == 2 and rep["activity"]["vm.create:denied"] == 1
    j = client.get("/audit/journal?node=hv101&tool=ip&lines=5", headers=H["tari"])
    assert j.status_code == 200 and "geneve" in j.text
    assert client.get("/audit/journal?node=hv101;reboot", headers=H["tari"]).status_code == 422
    assert client.post("/provider/tenants", headers=H["tari"], json={}).status_code == 403   # read only
    assert client.get("/carrier/underlay", headers=H["tari"]).status_code == 200


def test_failed_create_shows_error_and_frees_quota(client):
    before = client.get("/tenant/me", headers=H["alice"]).json()["usage"]["vms"]
    r = client.post("/tenant/vms", headers=H["alice"], json=dict(name="boom1", flavor="small"))
    assert r.status_code == 202
    run_jobs()
    j = client.get(r.json()["status_url"], headers=H["alice"]).json()
    assert j["state"] == "failed" and "512 MB" in j["error"]
    vm = client.get("/tenant/vms/boom1", headers=H["alice"]).json()
    assert vm["status"] == "error" and "512 MB" in vm["error"]
    assert client.get("/tenant/me", headers=H["alice"]).json()["usage"]["vms"] == before   # errors do not count
    assert client.delete("/tenant/vms/boom1", headers=H["alice"]).status_code == 202       # clean up the record
    run_jobs()
    assert client.get("/tenant/vms/boom1", headers=H["alice"]).status_code == 404


def test_delete_vm_releases_quota(client):
    assert client.delete("/tenant/vms/b2", headers=H["bob"]).status_code == 202
    run_jobs()
    assert client.get("/tenant/vms/b2", headers=H["bob"]).status_code == 404
    assert client.get("/tenant/me", headers=H["bob"]).json()["usage"]["vms"] == 1


def test_tenant_made_with_hubctl_is_invisible_until_imported(client, hub):
    # what happens when someone runs: hubctl tenant add tenant-x 150 10.150.20.0/24; hubctl vm add tenant-x x1
    hub.tenants["tenant-x"] = (150, "10.150.20.0/24")
    hub.vms["x1"] = dict(tenant="tenant-x", node="hv101", image="cirros", ip="10.150.20.10",
                         mem="512", cpus="1", mac="52:54:01:00:00:99", state="running")
    names = [t["name"] for t in client.get("/provider/tenants", headers=H["admin"]).json()]
    assert "tenant-x" not in names                                              # the API never heard of it
    un = client.get("/provider/unmanaged", headers=H["admin"]).json()
    assert [t["name"] for t in un["tenants"]] == ["tenant-x"] and [v["name"] for v in un["vms"]] == ["x1"]
    r = client.post("/provider/tenants", headers=H["admin"], json=dict(
        name="tenant-x", admin_username="xena", admin_password="xena-pass-1"))
    assert r.status_code == 409 and "/provider/import" in r.json()["detail"]
    r = client.post("/provider/import", headers=H["admin"]).json()
    assert r["tenants"] == ["tenant-x"] and r["vms"] == ["x1"]
    t = client.get("/provider/tenants/tenant-x", headers=H["admin"]).json()
    assert (t["vni"], t["subnet"], t["usage"]["vms"], t["usage"]["mem_mb"]) == (150, "10.150.20.0/24", 1, 512)
    assert client.get("/provider/unmanaged", headers=H["admin"]).json()["vms"] == []
    # give it an admin, who then sees the imported VM
    assert client.post("/provider/users", headers=H["admin"], json=dict(
        username="xena", password="xena-pass-1", role="consumer", tenant="tenant-x", tenant_admin=True)).status_code == 201
    hx = login(client, "xena", "xena-pass-1")
    vms = client.get("/tenant/vms", headers=hx).json()
    assert [(v["name"], v["flavor"], v["ip"], v["owner"]) for v in vms] == [("x1", "small", "10.150.20.10", "hubctl")]


def test_openapi_is_organised_by_ccra(client):
    spec = client.get("/openapi.json").json()
    tags = {t["name"] for t in spec["tags"]}
    assert {"Business support", "Provisioning & configuration", "Auditor", "Carrier"} <= tags
    assert "/tenant/vms" in spec["paths"] and "/provider/tenants" in spec["paths"]
