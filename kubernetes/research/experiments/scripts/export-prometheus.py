#!/usr/bin/env python3
import argparse
import requests
import pandas as pd

p = argparse.ArgumentParser()
p.add_argument('--prometheus', required=True)
p.add_argument('--namespace', required=True)
p.add_argument('--deployment', required=True)
p.add_argument('--start', required=True, type=int)
p.add_argument('--end', required=True, type=int)
p.add_argument('--step', required=True, type=int)
p.add_argument('--output', required=True)
a = p.parse_args()

prom = a.prometheus.rstrip('/')
pod_re = a.deployment + '-.*'
queries = {
    'cpu_cores': f'''sum(rate(container_cpu_usage_seconds_total{{namespace="{a.namespace}",pod=~"{pod_re}",container!="",container!="POD"}}[30s]))''',
    'memory_bytes': f'''sum(container_memory_working_set_bytes{{namespace="{a.namespace}",pod=~"{pod_re}",container!="",container!="POD"}})''',
    'network_rx_bytes_s': f'''sum(rate(container_network_receive_bytes_total{{namespace="{a.namespace}",pod=~"{pod_re}"}}[30s]))''',
    'network_tx_bytes_s': f'''sum(rate(container_network_transmit_bytes_total{{namespace="{a.namespace}",pod=~"{pod_re}"}}[30s]))''',
    'replicas': f'''kube_deployment_status_replicas{{namespace="{a.namespace}",deployment="{a.deployment}"}}''',
    'ready_replicas': f'''kube_deployment_status_replicas_available{{namespace="{a.namespace}",deployment="{a.deployment}"}}''',
}

def query_range(q):
    r = requests.get(prom + '/api/v1/query_range', params={
        'query': q, 'start': a.start, 'end': a.end, 'step': a.step
    }, timeout=30)
    r.raise_for_status()
    payload = r.json()
    result = payload.get('data', {}).get('result', [])
    if not result:
        return pd.DataFrame(columns=['timestamp','value'])
    # Aggregate across returned series if more than one exists.
    frames = []
    for s in result:
        df = pd.DataFrame(s.get('values', []), columns=['timestamp','value'])
        if df.empty:
            continue
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s', utc=True)
        df['value'] = pd.to_numeric(df['value'], errors='coerce')
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=['timestamp','value'])
    allv = pd.concat(frames, ignore_index=True)
    return allv.groupby('timestamp', as_index=False)['value'].sum()

merged = None
for name, q in queries.items():
    df = query_range(q).rename(columns={'value': name})
    merged = df if merged is None else pd.merge(merged, df, on='timestamp', how='outer')

if merged is None:
    merged = pd.DataFrame(columns=['timestamp'])
merged = merged.sort_values('timestamp')
merged.to_csv(a.output, index=False)
print(a.output)
