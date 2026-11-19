#!/usr/bin/env python3
# A custom metric for vdc_meter.py (vdc-portal): the Kubernetes clusters of vdc-k8s. Like a public
# cloud's cluster management fee, on top of the nodes' VM hours, which the kit already bills.
# vdc_meter.py runs it every round with VDC_TOKEN set (the meter's provider login); VDC_K8S is the
# add-on's address. One line per value: TENANT NAME VALUE.
#   "command": "python3 /path/to/vdc-k8s/examples/k8s_clusters.py clusters"   1 per ready cluster
#   "command": "python3 /path/to/vdc-k8s/examples/k8s_clusters.py nodes"      its number of nodes
import json, os, sys, urllib.request

what = sys.argv[1] if len(sys.argv) > 1 else "clusters"
url = os.environ.get("VDC_K8S", "http://127.0.0.1:8092") + "/addon/k8s/clusters"
req = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["VDC_TOKEN"]})
for c in json.load(urllib.request.urlopen(req, timeout=60)):
    if c["status"] not in ("ready", "scaling"):
        continue
    print(c["tenant"], "k8s:" + c["name"], 1 if what == "clusters" else len(c["nodes"]))
