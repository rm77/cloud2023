import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_tmp = tempfile.mkdtemp()
os.environ.update(VDC_DB=f"sqlite:///{_tmp}/test.db", VDC_JWT_SECRET="test-secret-" + "x" * 32,
                  VDC_ADMIN_USER="admin", VDC_ADMIN_PASSWORD="admin-pass-123")

import fakeredis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from rq import SimpleWorker  # noqa: E402

from app import services  # noqa: E402
from app.infra import labhub  # noqa: E402
from app.main import app  # noqa: E402
from tests.fakehub import FakeHub  # noqa: E402

services._redis = fakeredis.FakeRedis()


@pytest.fixture(scope="session")
def hub():
    h = FakeHub()
    labhub._run = h.run
    return h


@pytest.fixture(scope="session")
def client(hub):
    with TestClient(app) as c:
        yield c


def run_jobs():
    """what `rq worker` would do: run everything queued, then return"""
    SimpleWorker([services.queue()], connection=services._redis).work(burst=True)


def login(client, user, pw):
    r = client.post("/auth/token", data=dict(username=user, password=pw))
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}
