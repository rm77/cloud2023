# The job queue lives in the API's database: no Redis, nothing outside the kit folder.
import sqlite3
import tempfile

from sqlalchemy import create_engine
from sqlmodel import Session

from app import db, worker
from app.db import Job, now
from tests.conftest import login


def test_health_reports_the_worker(client):
    admin = login(client, "admin", "admin-pass-123")
    assert "worker not running" in client.get("/provider/health", headers=admin).json()["queue"]
    worker.beat()
    h = client.get("/provider/health", headers=admin).json()
    assert h["queue"] == "ok" and h["jobs_waiting"] == 0


def test_unfinished_job_fails_after_a_worker_restart():
    with Session(db.engine) as s:
        s.add(Job(id="vm-create-stuck", kind="vm.create", username="x", func="create_vm", state="started",
                  started=now()))
        s.commit()
    worker.recover()
    with Session(db.engine) as s:
        j = s.get(Job, "vm-create-stuck")
        assert j.state == "failed" and "worker stopped" in j.error


def test_unknown_task_is_refused():
    with Session(db.engine) as s:
        s.add(Job(id="evil-1", kind="x", username="x", func="os.system", kwargs='{"command": "id"}'))
        s.commit()
    worker.run_pending()
    with Session(db.engine) as s:
        assert s.get(Job, "evil-1").state == "failed"


def test_old_database_gets_new_columns(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE job (id VARCHAR PRIMARY KEY, kind VARCHAR, target VARCHAR, tenant_id INTEGER, "
              "username VARCHAR, created DATETIME)")
    c.execute("INSERT INTO job VALUES ('old-1', 'vm.create', 'a1', NULL, 'alice', '2026-11-01 10:00:00')")
    c.commit(); c.close()
    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    db.init_db()
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(job)")}
    assert {"func", "kwargs", "state", "result", "error"} <= cols
    assert sqlite3.connect(path).execute("SELECT state FROM job WHERE id='old-1'").fetchone()[0] == "unknown"


def test_redis_backend_runs_jobs_through_rq(client, monkeypatch):
    """the default backend: the API puts the job on Redis, an RQ worker calls run_job(id)"""
    import fakeredis
    from rq import SimpleWorker
    from app import config, services
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(config, "QUEUE_BACKEND", "redis")
    monkeypatch.setattr(services, "_redis", fake)
    admin = login(client, "admin", "admin-pass-123")
    r = client.post("/provider/nodes", headers=admin, json={"count": 1})
    assert r.status_code == 202, r.text
    jid = r.json()["job"]
    assert services.rq_queue().job_ids == [jid]
    SimpleWorker([services.rq_queue()], connection=fake).work(burst=True)
    j = client.get(f"/provider/jobs/{jid}", headers=admin).json()
    assert j["state"] == "finished", j
    worker.beat()
    h = client.get("/provider/health", headers=admin).json()
    assert h["queue"] == "ok" and h["queue_backend"] == "redis"


def test_redis_down_gives_503(client, monkeypatch):
    import redis
    from app import config, services
    monkeypatch.setattr(config, "QUEUE_BACKEND", "redis")
    monkeypatch.setattr(services, "_redis", redis.from_url("unix:///nonexistent/redis.sock"))
    admin = login(client, "admin", "admin-pass-123")
    r = client.post("/provider/nodes", headers=admin, json={"count": 1})
    assert r.status_code == 503 and "Redis" in r.json()["detail"]
    assert "Redis not reachable" in client.get("/provider/health", headers=admin).json()["queue"]
