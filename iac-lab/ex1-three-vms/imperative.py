#!/usr/bin/env python3
"""imperative.py - example 1, the imperative way: a list of commands, run in order.

    python ex1-three-vms/imperative.py

It says HOW: create app1, then app2, then test1. It doesn't know what already exists, so a
second run fails (the names are taken), and it can't notice or repair anything that changed.
Compare with tenant.yaml + vdc.py, which say WHAT should exist.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vdc_api import ApiError, Vdc  # noqa: E402

api = Vdc()

commands = [
    ("create app1",  "POST", "/tenant/vms", {"name": "app1",  "flavor": "small", "image": "alpine"}),
    ("create app2",  "POST", "/tenant/vms", {"name": "app2",  "flavor": "small", "image": "alpine"}),
    ("create test1", "POST", "/tenant/vms", {"name": "test1", "flavor": "tiny",  "image": "cirros"}),
]
for what, method, path, body in commands:
    try:
        api.job(method, path, json=body)
        print(f"{what}: done")
    except ApiError as e:
        sys.exit(f"{what}: FAILED - {e}")
