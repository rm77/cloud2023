# metering.py - CCRA metering and accounting.
#   python -m app.metering            sample every VDC_METER_MINUTES (default 5) forever
#   python -m app.metering --once     one sample (for cron or a systemd timer)
# Each sample adds, for every VM the hub reports as running, one row worth INTERVAL minutes.
import argparse
import time
from collections import defaultdict
from datetime import datetime, timedelta

from sqlmodel import Session, select

from . import config
from .db import Flavor, Tenant, UsageSample, Vm, engine, init_db, now, utc
from .infra import labhub


def sample_once(minutes=None):
    minutes = minutes or config.METER_INTERVAL
    states = {r["guest"]: r["state"] for r in labhub.table(labhub.hub("vms"))}
    n = 0
    with Session(engine) as s:
        flavors = {f.name: f for f in s.exec(select(Flavor))}
        tenants = {t.id: t.name for t in s.exec(select(Tenant))}
        for v in s.exec(select(Vm)):
            if states.get(v.name) != "running":
                continue
            f = flavors.get(v.flavor)
            if f is None:
                continue
            s.add(UsageSample(tenant=tenants.get(v.tenant_id, "?"), vm=v.name, flavor=v.flavor,
                              vcpus=f.vcpus, mem_mb=f.mem_mb, minutes=minutes,
                              cost=round(f.price_per_hour * minutes / 60, 6)))
            n += 1
        s.commit()
    return n


def report(s: Session, since: datetime | None = None, until: datetime | None = None, tenant: str | None = None):
    until = utc(until) or now()
    since = utc(since) or (until - timedelta(days=30))
    q = select(UsageSample).where(UsageSample.ts >= since, UsageSample.ts <= until)
    if tenant:
        q = q.where(UsageSample.tenant == tenant)
    acc = defaultdict(lambda: dict(minutes=0, vcpu_min=0, mb_min=0, cost=0.0, flavor=""))
    for u in s.exec(q):
        a = acc[(u.tenant, u.vm)]
        a["minutes"] += u.minutes
        a["vcpu_min"] += u.vcpus * u.minutes
        a["mb_min"] += u.mem_mb * u.minutes
        a["cost"] += u.cost
        a["flavor"] = u.flavor
    lines = [dict(tenant=t, vm=v, flavor=a["flavor"], vm_hours=round(a["minutes"] / 60, 2),
                  vcpu_hours=round(a["vcpu_min"] / 60, 2), ram_gb_hours=round(a["mb_min"] / 1024 / 60, 2),
                  cost=round(a["cost"], 4)) for (t, v), a in sorted(acc.items())]
    return dict(since=since, until=until, lines=lines, total_cost=round(sum(x["cost"] for x in lines), 4))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="vdc-cloud usage sampler")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    init_db()
    while True:
        try:
            print(f"{now():%F %T} sampled {sample_once()} running VM(s)", flush=True)
        except labhub.LabError as e:
            print(f"{now():%F %T} hub not reachable: {e}", flush=True)
        if a.once:
            break
        time.sleep(config.METER_INTERVAL * 60)
