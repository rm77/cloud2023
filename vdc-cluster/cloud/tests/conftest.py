import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_tmp = tempfile.mkdtemp()
# a stand-in for cloudflared: prints a trycloudflare address like the real one, then waits
_cf = os.path.join(_tmp, "cloudflared")
with open(_cf, "w") as f:
    f.write("""#!/bin/sh
origin=""; while [ $# -gt 0 ]; do [ "$1" = --url ] && origin=$2; shift; done
echo "INF Requesting new quick Tunnel on trycloudflare.com..."
case $origin in *:9999) echo 'failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": no such host'; exit 1 ;; esac
echo "INF +--------------------------------------------------------------------------------------------+"
echo "INF |  https://fake-$$-words.trycloudflare.com                                                     |"
echo "$origin" > "$PWD/origin.$$"
while :; do sleep 1; done
""")
os.chmod(_cf, 0o755)
os.environ.update(VDC_CLOUDFLARED=_cf, VDC_TUNNEL_DIR=os.path.join(_tmp, "tunnels"), VDC_QUEUE_BACKEND="sqlite", VDC_DB=f"sqlite:///{_tmp}/test.db", VDC_JWT_SECRET="test-secret-" + "x" * 32,
                  VDC_ADMIN_USER="admin", VDC_ADMIN_PASSWORD="admin-pass-123")

from fastapi.testclient import TestClient  # noqa: E402

from app import worker  # noqa: E402
from app.infra import labhub  # noqa: E402
from app.main import app  # noqa: E402
from tests.fakehub import FakeHub  # noqa: E402



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
    """what the worker would do: run everything queued, then return"""
    worker.run_pending()


def login(client, user, pw):
    r = client.post("/auth/token", data=dict(username=user, password=pw))
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest.fixture(scope="session")
def fake_ttyd(hub):
    """a stand-in for ttyd on the hub: its page, /token and the 'tty' WebSocket (echo)"""
    import base64
    import socket
    import threading
    import time

    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route, WebSocketRoute

    def authorized(sid, headers):
        port, guest, cred = hub.consoles.get(sid, (0, "", ""))
        return headers.get("authorization") == "Basic " + base64.b64encode(f"lab:{cred}".encode()).decode()

    async def page(request):
        sid = request.path_params["sid"]
        if not authorized(sid, request.headers):
            return PlainTextResponse("401", status_code=401)
        return PlainTextResponse(f"ttyd page of {hub.consoles[sid][1]}", headers={"content-type": "text/html"})

    async def token(request):
        if not authorized(request.path_params["sid"], request.headers):
            return PlainTextResponse("401", status_code=401)
        return JSONResponse({"token": "t0k"})

    async def tty(ws):
        if not authorized(ws.path_params["sid"], ws.headers):
            await ws.close(code=4401); return
        await ws.accept(subprotocol="tty")
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                return
            if m.get("bytes") is not None:
                await ws.send_bytes(b"echo:" + m["bytes"])
            else:
                await ws.send_text("echo:" + m["text"])

    app_ = Starlette(routes=[Route("/console/{sid}/token", token), Route("/console/{sid}/", page),
                             WebSocketRoute("/console/{sid}/ws", tty)])
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    server = uvicorn.Server(uvicorn.Config(app_, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    labhub.HUB_IP = "127.0.0.1"
    hub.console_port = port
    yield port
    server.should_exit = True
