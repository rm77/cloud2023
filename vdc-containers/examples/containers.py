#!/usr/bin/env python3
# A custom metric for vdc_meter.py (vdc-portal): the vCPUs reserved by each running container of
# vdc-containers (its --cpus). vdc_meter.py runs it every round with VDC_TOKEN set (the meter's
# provider login); VDC_CONTAINERS is the add-on's address. One line per value: TENANT NAME VALUE.
#   "command": "python3 /path/to/vdc-containers/examples/containers.py cpus"     vCPU of each container
#   "command": "python3 /path/to/vdc-containers/examples/containers.py mem_gb"   GB of memory of each
#   "command": "python3 /path/to/vdc-containers/examples/containers.py count"    1 per container
import json, os, sys, urllib.request

what = sys.argv[1] if len(sys.argv) > 1 else "cpus"
url = os.environ.get("VDC_CONTAINERS", "http://127.0.0.1:8091") + "/addon/containers"
req = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["VDC_TOKEN"]})
for c in json.load(urllib.request.urlopen(req, timeout=60)):
    if c["status"] != "running":
        continue
    value = {"cpus": c["cpus"], "mem_gb": c["mem_mb"] / 1024, "count": 1}[what]
    print(c["tenant"], "ctr:" + c["name"], round(value, 4))
