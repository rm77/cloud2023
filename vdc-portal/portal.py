#!/usr/bin/env python3
"""vdc-portal: serves the web console (index.html) and passes its API calls on to the cloud API
of vdc-cluster. Browser and API then share one origin, so the kit's API needs no change (no CORS).

    python3 portal.py --api http://<lab-pc>:36100            # console on http://<this host>:8080/
    python3 portal.py --api http://<lab-pc>:36100 --port 8090 --bind 127.0.0.1
    python3 portal.py --api http://<lab-pc>:36100 --meter-config metrics.json   # with vdc_meter.py
    python3 portal.py --api http://<lab-pc>:36100 --addon containers=http://127.0.0.1:8091   # add-on services

Python 3.8+ standard library only: nothing to install.
Web consoles (ttyd, WebSocket) and /docs are not passed through: the browser opens them on the
API's own address, which the page learns from /config.json.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vdc_meter  # noqa: E402  (the add-on meter's report, for /addon/usage)

HERE = os.path.dirname(os.path.abspath(__file__))
API_PREFIXES = ("/auth/", "/tenant/", "/provider/", "/audit/", "/carrier/")
PASS_HEADERS = ("authorization", "content-type", "accept")           # browser -> API
BACK_HEADERS = ("content-type", "www-authenticate", "location")      # API -> browser


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None                                                   # hand redirects back as they are


OPENER = urllib.request.build_opener(NoRedirect)


class Handler(BaseHTTPRequestHandler):
    api = ""
    public = ""
    meter_cfg = None                                                  # from --meter-config
    addons = {}                                                       # --addon NAME=URL: /addon/NAME/... -> URL
    whoami = {}                                                       # token -> (expires, /auth/me)
    timeout = 120
    server_version = "vdc-portal"

    def log_message(self, fmt, *args):
        if not getattr(self.server, "quiet", False):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def send(self, code, body, ctype, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def page(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/config.json":
            return self.send(200, json.dumps({"api": self.public}).encode(), "application/json")
        return self.send(404, b'{"detail":"not found"}', "application/json")

    def proxy(self, base=None, what="the cloud API"):
        base = base or self.api
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None
        req = urllib.request.Request(base + self.path, data=body, method=self.command)
        for k in PASS_HEADERS:
            if self.headers.get(k):
                req.add_header(k, self.headers[k])
        try:
            r = OPENER.open(req, timeout=self.timeout)
            code, data, hdrs = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:          # 4xx/5xx/3xx: pass the API's answer on unchanged
            code, data, hdrs = e.code, e.read(), e.headers
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            msg = json.dumps({"detail": f"portal: {what} at {base} does not answer ({reason})"})
            return self.send(502, msg.encode(), "application/json")
        self.send_response(code)
        for k in BACK_HEADERS:
            if hdrs.get(k):
                self.send_header(k, hdrs[k])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def me(self):
        """who is calling: the API answers /auth/me for the caller's own token (kept a minute)"""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        hit = Handler.whoami.get(auth)
        if hit and hit[0] > time.time():
            return hit[1]
        try:
            with OPENER.open(urllib.request.Request(self.api + "/auth/me", headers={"Authorization": auth}), timeout=30) as r:
                me = json.loads(r.read())
        except urllib.error.HTTPError:
            return None
        if len(Handler.whoami) > 1000:
            Handler.whoami.clear()
        Handler.whoami[auth] = (time.time() + 60, me)
        return me

    def addon(self):
        """the add-on meter's charges (vdc_meter.py); tenant users see only their own tenant"""
        jsend = lambda code, obj: self.send(code, json.dumps(obj).encode(), "application/json")
        if self.meter_cfg is None:
            return jsend(404, {"detail": "no add-on meter: start portal.py with --meter-config"})
        if self.command not in ("GET", "HEAD"):
            return jsend(405, {"detail": "method not allowed"})
        try:
            me = self.me()
        except (urllib.error.URLError, OSError) as e:
            return jsend(502, {"detail": f"portal: the cloud API at {self.api} does not answer ({e})"})
        if not me:
            return jsend(401, {"detail": "log in first"})
        path, _, qs = self.path.partition("?")
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(qs).items()}
        if path == "/addon/metrics":
            return jsend(200, {"interval_minutes": self.meter_cfg["interval_minutes"],
                               "metrics": vdc_meter.public_metrics(self.meter_cfg)})
        if path == "/addon/usage":
            tenant = me.get("tenant") if me.get("role") == "consumer" else (q.get("tenant") or None)
            if me.get("role") == "consumer" and not tenant:
                return jsend(403, {"detail": "no tenant"})
            try:
                return jsend(200, vdc_meter.report(self.meter_cfg, q.get("since"), q.get("until"), tenant))
            except ValueError as e:
                return jsend(422, {"detail": f"bad date: {e}"})
        return jsend(404, {"detail": "not found"})

    def route(self):
        if self.path.startswith(API_PREFIXES):
            return self.proxy()
        if self.path.startswith("/addon/"):
            name = self.path[len("/addon/"):].split("/")[0].split("?")[0]
            if name in self.addons:                  # an add-on service: passed on as it is, token and all
                return self.proxy(self.addons[name], f"the add-on {name}")
            if self.path.split("?")[0] == "/addon/addons":
                return self.send(200, json.dumps(sorted(self.addons)).encode(), "application/json")
            return self.addon()
        if self.command in ("GET", "HEAD"):
            return self.page()
        return self.send(405, b'{"detail":"method not allowed"}', "application/json")

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = route


def main():
    ap = argparse.ArgumentParser(description="web console for the vdc-cluster cloud API")
    ap.add_argument("--api", default=os.environ.get("VDC_API", "http://127.0.0.1:36100"),
                    help="the cloud API as this host reaches it (default $VDC_API or http://127.0.0.1:36100)")
    ap.add_argument("--public-api", default=os.environ.get("VDC_API_PUBLIC", ""),
                    help="the API as browsers reach it, for web consoles and /docs (default: same as --api)")
    ap.add_argument("--bind", default=os.environ.get("VDC_PORTAL_BIND", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("VDC_PORTAL_PORT", "8080")))
    ap.add_argument("--meter-config", default=os.environ.get("VDC_METER_CONFIG", ""),
                    help="metrics.json of vdc_meter.py: show the add-on charges in the console")
    ap.add_argument("--addon", action="append", default=[], metavar="NAME=URL",
                    help="an add-on service: /addon/NAME/... is passed on to URL (e.g. containers=http://127.0.0.1:8091); "
                         "repeat for more")
    ap.add_argument("--quiet", action="store_true", help="no request log")
    a = ap.parse_args()
    for spec in a.addon + [s for s in os.environ.get("VDC_PORTAL_ADDONS", "").split(",") if s]:
        name, _, url = spec.partition("=")
        if not name or not url.startswith("http") or name in ("metrics", "usage", "addons"):
            ap.error(f"--addon {spec}: NAME=http://host:port (not metrics, usage or addons)")
        Handler.addons[name] = url.rstrip("/")
    if a.meter_config:
        Handler.meter_cfg = vdc_meter.load_config(a.meter_config)
    Handler.api = a.api.rstrip("/")
    Handler.public = (a.public_api or a.api).rstrip("/")
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    srv.quiet = a.quiet
    print(f"vdc-portal on http://{a.bind}:{a.port}/  ->  API {Handler.api}  (browsers open consoles on {Handler.public})"
          + (f"; add-on meter {Handler.meter_cfg['db']}" if Handler.meter_cfg else "")
          + "".join(f"; /addon/{n} -> {u}" for n, u in Handler.addons.items()), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
