# VM power: start, stop, reboot - for the tenant on its own VMs, for the provider on any VM
from tests.conftest import login, run_jobs

H = {}


def job_state(client, h, url):
    return client.get(url, headers=h).json()


def test_setup(client, hub):
    H["admin"] = login(client, "admin", "admin-pass-123")
    for t, u in (("tenant-p", "pat"), ("tenant-q", "quinn")):
        r = client.post("/provider/tenants", headers=H["admin"], json=dict(
            name=t, admin_username=u, admin_password=u + "-pass-123"))
        assert r.status_code == 201, r.text
        H[u] = login(client, u, u + "-pass-123")
    assert client.post("/tenant/vms", headers=H["pat"], json={"name": "p1"}).status_code == 202
    run_jobs()
    assert client.get("/tenant/vms/p1", headers=H["pat"]).json()["state"] == "running"


def test_tenant_stop_start_reboot(client, hub):
    r = client.post("/tenant/vms/p1/stop", headers=H["pat"])
    assert r.status_code == 202, r.text
    run_jobs()
    j = job_state(client, H["pat"], r.json()["status_url"])
    assert j["kind"] == "vm.stop" and j["state"] == "finished" and j["result"]["action"] == "stop"
    assert client.get("/tenant/vms/p1", headers=H["pat"]).json()["state"] == "shut off"

    assert client.post("/tenant/vms/p1/start", headers=H["pat"]).status_code == 202
    run_jobs()
    assert client.get("/tenant/vms/p1", headers=H["pat"]).json()["state"] == "running"

    assert client.post("/tenant/vms/p1/reboot", headers=H["pat"]).status_code == 202
    run_jobs()
    assert hub.calls[-1].endswith("hubctl vm reboot p1") or any("vm reboot p1" in c for c in hub.calls)


def test_other_tenant_cannot_touch_it(client, hub):
    for action in ("start", "stop", "reboot"):
        assert client.post(f"/tenant/vms/p1/{action}", headers=H["quinn"]).status_code == 404


def test_provider_on_any_vm(client, hub):
    r = client.post("/provider/vms/p1/stop", headers=H["admin"])
    assert r.status_code == 202 and r.json()["status_url"].startswith("/provider/jobs/")
    run_jobs()
    assert hub.vms["p1"]["state"] == "shut off"
    assert client.post("/provider/vms/p1/explode", headers=H["admin"]).status_code == 422
    assert client.post("/provider/vms/nope/start", headers=H["admin"]).status_code == 404
    assert client.post("/provider/vms/p1/start", headers=H["pat"]).status_code == 403   # tenants can't use /provider
    # the provider's console route is not shadowed by /vms/{name}/{action}
    assert client.post("/provider/vms/p1/console", headers=H["admin"]).status_code != 422


def test_not_while_deleting_and_audited(client, hub):
    assert client.delete("/tenant/vms/p1", headers=H["pat"]).status_code == 202
    assert client.post("/tenant/vms/p1/start", headers=H["pat"]).status_code == 409   # before the worker ran
    run_jobs()
    events = client.get("/audit/events", headers=H["admin"]).json()
    actions = {(e["action"], e["target"]) for e in events}
    assert {("vm.stop", "p1"), ("vm.start", "p1"), ("vm.reboot", "p1")} <= actions
