# App Sample

This creates and deploys a small Flask/Gunicorn application into an existing kind cluster.

Default assumptions:

- kind cluster name: `kind`
- application namespace: `cloud-exp`
- application image: `experiment-app:v1`
- Ingress class: `nginx`
- application endpoint: `http://experiment.10.28.84.254.sslip.io`
- Prometheus endpoint: `http://prometheus.10.28.84.254.sslip.io`

## Contents

```sh
kind get clusters
```

If the cluster name is not `kind`, set it before running the scripts:

```sh
export KIND_CLUSTER=my-cluster
```

## Build and load the image into kind

```sh
./scripts/build-load.sh
```

This performs:

```sh
docker build -t experiment-app:v1 .
kind load docker-image experiment-app:v1 --name "$KIND_CLUSTER"
```

## Deploy

```sh
./scripts/deploy.sh
```

This creates the `cloud-exp` namespace and deploys:

- Deployment
- ClusterIP Service
- nginx Ingress

## Verify

```sh
./scripts/verify.sh
```

Or test manually:

```sh
curl http://experiment.10.28.84.254.sslip.io/
curl http://experiment.10.28.84.254.sslip.io/health
curl http://experiment.10.28.84.254.sslip.io/cpu
curl http://experiment.10.28.84.254.sslip.io/sleep
```

## 6. Generate a small manual load

```sh
./scripts/manual-load.sh
```

Change the number of concurrent requests:

```sh
REQUESTS=50 ./scripts/manual-load.sh
```

## 7. Verify Prometheus sees the application

CPU:

```sh
curl -sG       'http://prometheus.10.28.84.254.sslip.io/api/v1/query'       --data-urlencode       'query=sum(rate(container_cpu_usage_seconds_total{namespace="cloud-exp",container="app"}[1m]))'
```

Memory:

```sh
curl -sG       'http://prometheus.10.28.84.254.sslip.io/api/v1/query'       --data-urlencode       'query=container_memory_working_set_bytes{namespace="cloud-exp",container="app"}'
```

## One-command setup

If all prerequisites are already available:

```sh
./scripts/install-all.sh
```

## Override defaults

Different kind cluster:

```sh
KIND_CLUSTER=cloud ./scripts/build-load.sh
```

Different image:

```sh
IMAGE=experiment-app:v2 ./scripts/build-load.sh
```

Different application URL:

```sh
APP_HOST=http://other-host.example ./scripts/verify.sh
```

## Request behavior

- `/` lightweight request
- `/cpu` CPU-heavy endpoint
- `/sleep` 50 ms wait
- `/health` readiness/liveness endpoint

The `/cpu` endpoint is intended for the first workload-resource-performance experiment.
