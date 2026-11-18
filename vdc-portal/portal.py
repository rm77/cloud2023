#!/usr/bin/env python3
"""vdc-portal: serves the web console (index.html) and passes its API calls on to the cloud API
of vdc-cluster. Browser and API then share one origin, so the kit's API needs no change (no CORS).

    python3 portal.py --api http://<lab-pc>:36100            # console on http://<this host>:8080/
    python3 portal.py --api http://<lab-pc>:36100 --port 8090 --bind 127.0.0.1

Python 3.8+ standard library only: nothing to install.
Web consoles (ttyd, WebSocket) and /docs are not passed through: the browser opens them on the
API's own address, which the page learns from /config.json.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

    def proxy(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None
        req = urllib.request.Request(self.api + self.path, data=body, method=self.command)
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
            msg = json.dumps({"detail": f"portal: the cloud API at {self.api} does not answer ({reason})"})
            return self.send(502, msg.encode(), "application/json")
        self.send_response(code)
        for k in BACK_HEADERS:
            if hdrs.get(k):
                self.send_header(k, hdrs[k])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def route(self):
        if self.path.startswith(API_PREFIXES):
            return self.proxy()
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
    ap.add_argument("--quiet", action="store_true", help="no request log")
    a = ap.parse_args()
    Handler.api = a.api.rstrip("/")
    Handler.public = (a.public_api or a.api).rstrip("/")
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    srv.quiet = a.quiet
    print(f"vdc-portal on http://{a.bind}:{a.port}/  ->  API {Handler.api}  (browsers open consoles on {Handler.public})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
