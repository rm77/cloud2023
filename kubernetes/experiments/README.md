# Workload Experiments

assumes:
- a running kind cluster,
- ingress-nginx is already installed,
- Prometheus is reachable at `http://prometheus.10.28.84.254.sslip.io`,
- the experiment application is reachable at `http://experiment.10.28.84.254.sslip.io`.

## 1. Install local Python dependencies

```sh
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt
```

## 2. Deploy the experiment application

Create the namespace if needed:

```sh
kubectl create namespace cloud-exp --dry-run=client -o yaml | kubectl apply -f -
```

Build/load your app image as `cloud-exp-app:v1`, then:

```sh
kubectl apply -f k8s/experiment-app.yaml
kubectl get pods -n cloud-exp
curl http://experiment.10.28.84.254.sslip.io/
```

## 3. Verify Prometheus

```sh
curl http://prometheus.10.28.84.254.sslip.io/-/healthy
curl -sG 'http://prometheus.10.28.84.254.sslip.io/api/v1/query' \
  --data-urlencode 'query=up'
```

## 4. Run experiments

```sh
./scripts/run-experiment.sh run01 10
./scripts/run-experiment.sh run02 20
./scripts/run-experiment.sh run03 40
./scripts/run-experiment.sh run04 80
```

Environment variables can override defaults:

```sh
APP_HOST=http://experiment.10.28.84.254.sslip.io \
PROM_HOST=http://prometheus.10.28.84.254.sslip.io \
MEASUREMENT_SECONDS=300 \
./scripts/run-experiment.sh run05 100
```

## 5. Output

Each run produces raw files under:

```text
data/raw/<run_id>/
```

and a processed Level-3 dataset under:

```text
data/processed/<run_id>.csv
```

Typical columns include:
- timestamp
- run_id
- workload_level
- request_rate
- throughput_rps
- latency_mean_ms
- latency_p50_ms
- latency_p95_ms
- latency_p99_ms
- failure_rate
- error_fraction
- cpu_cores
- memory_bytes
- network_rx_bytes_s
- network_tx_bytes_s
- replicas
- ready_replicas

## 6. Research interpretation

The dataset supports analysis of:

`Workload -> Resource`

`Workload -> Performance`

`Resource -> Performance`

Keep warm-up, measurement duration, resource limits, image version, and Prometheus step constant when comparing workload levels.
