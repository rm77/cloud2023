#!/usr/bin/env python3
# A custom metric for vdc_meter.py: the number of user accounts of each tenant.
# vdc_meter.py runs it every round with VDC_API and VDC_TOKEN (the meter's provider login) set,
# and reads one line per value: TENANT VM VALUE ("-" as VM for a tenant-wide value).
import json, os, urllib.request
from collections import Counter

req = urllib.request.Request(os.environ["VDC_API"] + "/provider/users",
                             headers={"Authorization": "Bearer " + os.environ["VDC_TOKEN"]})
users = json.load(urllib.request.urlopen(req, timeout=60))
for tenant, n in sorted(Counter(u["tenant"] for u in users if u.get("tenant")).items()):
    print(tenant, "-", n)
