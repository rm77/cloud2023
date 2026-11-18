#!/usr/bin/env python3
"""vdc.py - infrastructure as code for one vdc-cluster tenant, in about 150 lines.

    python vdc.py plan       [-f tenant.yaml]   what would change, nothing is touched
    python vdc.py apply      [-f tenant.yaml]   make the tenant match the file (asks first; -y: don't)
    python vdc.py destroy    [-f tenant.yaml]   delete the VMs the file declares
    python vdc.py show                          what exists now

The file says what should exist (desired state). Every run reads what does exist (actual
state) from the cloud API, compares the two and lists the differences:
    +  create      -  delete      ~  replace (flavor or image changed: VMs can't be changed in place)
    >  start       .  stop        !  recreate (the VM is in error)
The API is the source of truth for what exists. A small state file next to tenant.yaml
(.vdc-state.json, like Terraform's terraform.tfstate) remembers which VMs and keys this file
created, so it deletes only those - other VMs of the tenant are left alone and listed as
"not managed by this file".
"""
import argparse
import base64
import hashlib
import json
import os
import sys

import yaml

from vdc_api import ApiError, Vdc


# ---------------------------------------------------------------- desired state (the file)
def load(path):
    with open(path) as f:
        spec = yaml.safe_load(f) or {}
    vms = {}
    for v in spec.get("vms", []):
        power = v.get("power", "on")
        power = {True: "on", False: "off"}.get(power, power)    # YAML reads a bare on/off as true/false
        if power not in ("on", "off"):
            sys.exit(f"{v['name']}: power must be on or off")
        vms[v["name"]] = dict(flavor=v.get("flavor", "tiny"), image=v.get("image", "cirros"), power=power)
    keys = {}
    for k in spec.get("keys", []):
        text = open(os.path.expanduser(k)).read().strip() if not k.startswith("ssh-") else k
        keys[fingerprint(text)] = text
    return keys, vms


def fingerprint(public_key):
    """SHA256:... the same way ssh-keygen -l and the API print it"""
    blob = base64.b64decode(public_key.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


# ---------------------------------------------------------------- actual state (the API)
def actual(api):
    keys = {k["fingerprint"]: k for k in api.get("/tenant/keys")}
    vms = {}
    for v in api.get("/tenant/vms", params={"state": "true"}):
        vms[v["name"]] = dict(flavor=v["flavor"], image=v["image"], status=v["status"],
                              power="on" if v.get("state") == "running" else "off", ip=v.get("ip"),
                              ssh=v.get("ssh"))
    return keys, vms


# ---------------------------------------------------------------- what this file manages
def state_path(spec_file):
    return os.path.join(os.path.dirname(os.path.abspath(spec_file)), ".vdc-state.json")


def load_state(spec_file):
    try:
        with open(state_path(spec_file)) as f:
            st = json.load(f)
        return set(st.get("vms", [])), set(st.get("keys", []))
    except FileNotFoundError:
        return set(), set()


def save_state(spec_file, vms, keys):
    with open(state_path(spec_file), "w") as f:
        json.dump({"vms": sorted(vms), "keys": sorted(keys)}, f, indent=2)


# ---------------------------------------------------------------- the difference
def diff(want_keys, want_vms, have_keys, have_vms, managed_vms, managed_keys, destroy=False):
    steps = []                                   # (symbol, kind, name, detail)
    if destroy:
        return [("-", "vm", n, "") for n in want_vms if n in have_vms]
    for fp in want_keys:
        if fp not in have_keys:
            steps.append(("+", "key", fp, want_keys[fp].split()[-1]))
    for fp, k in have_keys.items():
        if fp not in want_keys and fp in managed_keys:       # only keys this file added
            steps.append(("-", "key", fp, k.get("comment", "")))
    for n, w in want_vms.items():
        h = have_vms.get(n)
        if h is None:
            steps.append(("+", "vm", n, f"{w['flavor']}, {w['image']}" + (", stays off" if w["power"] == "off" else "")))
        elif h["status"] == "error":
            steps.append(("!", "vm", n, "in error: delete and create again"))
        elif (h["flavor"], h["image"]) != (w["flavor"], w["image"]):
            steps.append(("~", "vm", n, f"{h['flavor']}, {h['image']} -> {w['flavor']}, {w['image']}"))
        elif h["status"] == "active" and h["power"] != w["power"]:
            steps.append((">" if w["power"] == "on" else ".", "vm", n, f"power {h['power']} -> {w['power']}"))
    for n in have_vms:
        if n not in want_vms and n in managed_vms:              # only VMs this file created
            steps.append(("-", "vm", n, "removed from the file"))
    return steps


# ---------------------------------------------------------------- making it so
def run(api, steps, want_keys, want_vms):
    # keys first: a VM gets the keys that exist when it is created
    for sym, kind, name, _ in steps:
        if kind == "key" and sym == "+":
            api.call("POST", "/tenant/keys", json={"key": want_keys[name]})
        elif kind == "key" and sym == "-":
            kid = next(k["id"] for k in api.get("/tenant/keys") if k["fingerprint"] == name)
            api.call("DELETE", f"/tenant/keys/{kid}")
        else:
            continue
        print(f"  {sym} key {name}: done")
    # then deletions (they free quota), then creations, then power
    order = {"-": 0, "!": 1, "~": 1, "+": 2, ">": 3, ".": 3}
    for sym, kind, name, _ in sorted((s for s in steps if s[1] == "vm"), key=lambda s: order[s[0]]):
        if sym in "-!~":
            api.job("DELETE", f"/tenant/vms/{name}")
        if sym in "+!~":
            w = want_vms[name]
            api.job("POST", "/tenant/vms", json={"name": name, "flavor": w["flavor"], "image": w["image"]})
            if w["power"] == "off":
                api.job("POST", f"/tenant/vms/{name}/stop")
        if sym in ">.":
            api.job("POST", f"/tenant/vms/{name}/{'start' if sym == '>' else 'stop'}")
        print(f"  {sym} vm {name}: done")


def main():
    p = argparse.ArgumentParser(description="infrastructure as code for a vdc-cluster tenant")
    p.add_argument("command", choices=["plan", "apply", "destroy", "show"])
    p.add_argument("-f", "--file", default="tenant.yaml")
    p.add_argument("-y", "--yes", action="store_true", help="apply/destroy without asking")
    a = p.parse_args()

    api = Vdc()
    have_keys, have_vms = actual(api)
    if a.command == "show":
        for n, v in sorted(have_vms.items()):
            print(f"{n:12} {v['status']:9} {v['power']:4} {v['flavor']:8} {v['image']:10} {v['ip'] or '-':16} {v['ssh'] or ''}")
        print(f"{len(have_keys)} key(s)")
        return

    want_keys, want_vms = load(a.file)
    managed_vms, managed_keys = load_state(a.file)
    steps = diff(want_keys, want_vms, have_keys, have_vms, managed_vms, managed_keys,
                 destroy=a.command == "destroy")
    for sym, kind, name, detail in steps:
        print(f"{sym} {kind} {name}" + (f"  ({detail})" if detail else ""))
    others = sorted(n for n in have_vms if n not in want_vms and n not in managed_vms)
    if others:
        print(f"  (not managed by this file, left alone: {', '.join(others)})")
    if not steps:
        print("no changes: the tenant matches the file")
        if a.command == "apply":
            save_state(a.file, set(want_vms), set(want_keys))
        return
    counts = {s: sum(1 for x in steps if x[0] == s) for s in "+-~!>."}
    print("plan: " + ", ".join(f"{c} {w}" for s, w in (("+", "to create"), ("~", "to replace"), ("!", "to recreate"),
                                                       ("-", "to delete"), (">", "to start"), (".", "to stop"))
                               if (c := counts[s])))
    if a.command == "plan":
        return
    if not a.yes and input("go ahead? (yes/no) ").strip() != "yes":
        print("nothing done")
        return
    try:
        run(api, steps, want_keys, want_vms)
    except ApiError as e:
        sys.exit(f"stopped: {e}\nrun 'python vdc.py plan' to see what is left")
    finally:
        # remember what this file manages now (what exists of it after the run)
        have_keys, have_vms = actual(api)
        if a.command == "destroy":
            save_state(a.file, managed_vms & set(have_vms), managed_keys & set(have_keys))
        else:
            save_state(a.file, (set(want_vms) | managed_vms) & set(have_vms), set(want_keys) & set(have_keys))
    print("done")


if __name__ == "__main__":
    main()
