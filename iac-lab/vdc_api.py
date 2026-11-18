"""vdc_api.py - a small client for the vdc-cluster cloud API, shared by vdc.py and inventory.py.

Settings come from the environment, so no password ends up in Git:
    VDC_API    the API, e.g. http://10.28.84.254:36100
    VDC_USER   a tenant user, e.g. alice
    VDC_PASS   its password
"""
import os
import sys
import time

import requests


class ApiError(Exception):
    pass


class Vdc:
    def __init__(self, api=None, user=None, password=None):
        self.api = (api or os.environ.get("VDC_API", "http://10.28.84.254:36100")).rstrip("/")
        user = user or os.environ.get("VDC_USER")
        password = password or os.environ.get("VDC_PASS")
        if not user or not password:
            sys.exit("set VDC_USER and VDC_PASS (and VDC_API) first")
        self.s = requests.Session()
        r = self.s.post(f"{self.api}/auth/token", data={"username": user, "password": password}, timeout=30)
        if r.status_code != 200:
            sys.exit(f"login as {user} failed: {self._detail(r)}")
        self.s.headers["Authorization"] = "Bearer " + r.json()["access_token"]

    # ---- plain HTTP, with the API's own error message on failure
    @staticmethod
    def _detail(r):
        try:
            return f"{r.status_code} {r.json().get('detail')}"
        except ValueError:
            return f"{r.status_code} {r.text[:200]}"

    def call(self, method, path, **kw):
        r = self.s.request(method, f"{self.api}{path}", timeout=60, **kw)
        if r.status_code >= 400:
            raise ApiError(f"{method} {path}: {self._detail(r)}")
        return r.json() if r.content else None

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    # ---- every change to a VM is a job: start it, then wait for the worker to finish it
    def job(self, method, path, **kw):
        status_url = self.call(method, path, **kw)["status_url"]
        while True:
            j = self.get(status_url)
            if j["state"] in ("finished", "failed", "unknown"):
                if j["state"] != "finished":
                    raise ApiError(f"{j['kind']} {j['target']}: {j['state']} {j.get('error') or ''}".strip())
                return j
            time.sleep(2)
