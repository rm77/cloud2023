# /console/<id>/ - the browser side of a web console. The page and its WebSocket are
# passed through to ttyd on the hub. The id in the URL is the secret (32 hex, from
# POST /tenant/vms/{name}/console): one browser session, until it expires. The API adds
# ttyd's basic-auth credential, so ttyd itself is never reachable without the API.
import asyncio
import base64

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import RedirectResponse, Response
from sqlmodel import Session
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from ..db import ConsoleSession, engine, now
from ..infra import labhub

router = APIRouter(include_in_schema=False)
PASS_HEADERS = ("content-type", "cache-control", "etag", "last-modified")


def live(sid: str) -> ConsoleSession:
    if not (len(sid) == 32 and all(c in "0123456789abcdef" for c in sid)):
        raise HTTPException(404, "no such console")
    with Session(engine) as s:
        cs = s.get(ConsoleSession, sid)
        if cs is None or cs.closed or cs.expires <= now():
            raise HTTPException(404, "this console has expired or was closed: open a new one")
        s.expunge(cs)
        return cs


def upstream(cs: ConsoleSession, path: str, scheme="http") -> str:
    return f"{scheme}://{labhub.HUB_IP}:{cs.port}/console/{cs.id}/{path}"


def auth(cs: ConsoleSession) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"lab:{cs.cred}".encode()).decode()}


def mark(sid: str, **fields):
    with Session(engine) as s:
        cs = s.get(ConsoleSession, sid)
        if cs:
            for k, v in fields.items():
                setattr(cs, k, v)
            s.add(cs); s.commit()


@router.get("/console/{sid}")
def console_slash(sid: str):
    return RedirectResponse(f"/console/{sid}/")


@router.get("/console/{sid}/{path:path}")
async def console_http(sid: str, path: str, request: Request):
    cs = live(sid)
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(upstream(cs, path), params=dict(request.query_params), headers=auth(cs))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"web console not reachable on the hub: {e.__class__.__name__}")
    headers = {k: v for k, v in r.headers.items() if k.lower() in PASS_HEADERS}
    return Response(content=r.content, status_code=r.status_code, headers=headers)


@router.websocket("/console/{sid}/ws")
async def console_ws(ws: WebSocket, sid: str):
    try:
        cs = live(sid)
    except HTTPException:
        await ws.close(code=4404)
        return
    wanted = ws.scope.get("subprotocols") or []
    sub = "tty" if "tty" in wanted else None
    try:
        up = await ws_connect(upstream(cs, "ws", "ws"), subprotocols=[sub] if sub else None,
                              additional_headers=auth(cs), max_size=None, open_timeout=10)
    except Exception:                    # hub unreachable, ttyd gone, refused
        await ws.close(code=1011)
        return
    await ws.accept(subprotocol=sub)
    mark(sid, used=True)

    async def browser_to_hub():
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                return
            if m.get("bytes") is not None:
                await up.send(m["bytes"])
            elif m.get("text") is not None:
                await up.send(m["text"])

    async def hub_to_browser():
        try:
            async for m in up:
                if isinstance(m, bytes):
                    await ws.send_bytes(m)
                else:
                    await ws.send_text(m)
        except ConnectionClosed:
            pass

    tasks = [asyncio.create_task(browser_to_hub()), asyncio.create_task(hub_to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        mark(sid, closed=True)          # ttyd runs with --once: this session is over
        for t in tasks:
            t.cancel()
        try:
            await up.close()
            await ws.close()
        except Exception:               # the browser or ttyd is already gone
            pass
