#!/usr/bin/env python3
"""inventory.py [OUT] - writes an Ansible inventory (default: hosts.yml) from the cloud API
(GET /tenant/access).

    python inventory.py ex2-web-server/hosts.yml

Every VM has its own SSH port on the lab PC's campus address, so Ansible reaches it directly:
    web1  ansible_host=10.28.84.254  ansible_port=46000  ansible_user=alpine
Groups come from the names: web* -> web, db* -> db, the rest -> other.
"""
import os
import sys

import yaml

from vdc_api import Vdc

api = Vdc()
a = api.get("/tenant/access")
host = os.environ.get("VDC_SSH_HOST") or a["jump_host"]     # the lab PC's campus address
groups = {"web": {}, "db": {}, "other": {}}
skipped = []
for g in a["guests"]:
    if not g.get("ssh_port"):
        skipped.append(g["name"])
        continue
    group = "web" if g["name"].startswith("web") else "db" if g["name"].startswith("db") else "other"
    groups[group][g["name"]] = {"ansible_host": host, "ansible_port": g["ssh_port"],
                                "ansible_user": g["user"], "private_ip": g["ip"]}

inventory = {"all": {
    "vars": {
        # Alpine: become root with doas (cloud-init gives the default user 'permit nopass')
        "ansible_become_method": "community.general.doas",
        "ansible_python_interpreter": "/usr/bin/python3",
        # each recreated VM has a new host key on the same port
        "ansible_ssh_common_args": "-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=.vdc_known_hosts",
    },
    "children": {name: {"hosts": hosts} for name, hosts in groups.items() if hosts},
}}
out = sys.argv[1] if len(sys.argv) > 1 else "hosts.yml"
with open(out, "w") as f:
    yaml.safe_dump(inventory, f, sort_keys=False)
n = sum(len(h) for h in groups.values())
print(f"{out}: {n} host(s)" + (f"; no SSH port: {', '.join(skipped)}" if skipped else ""))
