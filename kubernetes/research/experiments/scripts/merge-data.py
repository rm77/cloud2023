#!/usr/bin/env python3
import argparse
import pandas as pd

p = argparse.ArgumentParser()
p.add_argument('--run-id', required=True)
p.add_argument('--workload-level', required=True, type=int)
p.add_argument('--locust', required=True)
p.add_argument('--prometheus', required=True)
p.add_argument('--events', required=False)
p.add_argument('--output', required=True)
a = p.parse_args()

loc = pd.read_csv(a.locust)
# Locust history column names vary slightly by version.
if 'Timestamp' not in loc.columns:
    raise SystemExit('Locust history CSV has no Timestamp column')
loc['timestamp'] = pd.to_datetime(loc['Timestamp'], unit='s', utc=True)

# Prefer aggregate row if present.
if 'Name' in loc.columns and (loc['Name'] == 'Aggregated').any():
    loc = loc[loc['Name'] == 'Aggregated'].copy()
elif 'Name' in loc.columns and (loc['Name'] == 'Total').any():
    loc = loc[loc['Name'] == 'Total'].copy()

rename = {}
for c in loc.columns:
    lc = c.lower().strip()
    if lc in ('requests/s','current rps'):
        rename[c] = 'request_rate'
    elif lc in ('failures/s','current failures/s'):
        rename[c] = 'failure_rate'
    elif lc in ('average response time','avg response time'):
        rename[c] = 'latency_mean_ms'
    elif lc == '50%':
        rename[c] = 'latency_p50_ms'
    elif lc == '95%':
        rename[c] = 'latency_p95_ms'
    elif lc == '99%':
        rename[c] = 'latency_p99_ms'
loc = loc.rename(columns=rename)

keep = ['timestamp'] + [c for c in [
    'request_rate','failure_rate','latency_mean_ms',
    'latency_p50_ms','latency_p95_ms','latency_p99_ms'
] if c in loc.columns]
loc = loc[keep].sort_values('timestamp')

prom = pd.read_csv(a.prometheus)
prom['timestamp'] = pd.to_datetime(prom['timestamp'], utc=True)
prom = prom.sort_values('timestamp')

# Align Locust and Prometheus on nearest timestamp.
out = pd.merge_asof(
    loc, prom, on='timestamp', direction='nearest', tolerance=pd.Timedelta('3s')
)
out['run_id'] = a.run_id
out['workload_level'] = a.workload_level
if 'failure_rate' in out.columns and 'request_rate' in out.columns:
    denom = out['request_rate'] + out['failure_rate']
    out['error_fraction'] = (out['failure_rate'] / denom.where(denom != 0)).fillna(0)
if 'request_rate' in out.columns:
    out['throughput_rps'] = out['request_rate']
out.to_csv(a.output, index=False)
print(a.output)
