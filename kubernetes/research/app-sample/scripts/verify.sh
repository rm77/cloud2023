#!/bin/sh
set -eu

APP_HOST="${APP_HOST:-http://experiment.10.28.84.254.sslip.io}"
PROM_HOST="${PROM_HOST:-http://prometheus.10.28.84.254.sslip.io}"

echo "=== DNS ==="
getent hosts experiment.10.28.84.254.sslip.io || true

echo
echo "=== Application root ==="
curl -fsS "${APP_HOST}/"

echo
echo "=== Health ==="
curl -fsS "${APP_HOST}/health"
echo

echo
echo "=== CPU endpoint ==="
curl -fsS "${APP_HOST}/cpu"
echo

echo
echo "=== Prometheus memory query ==="
curl -sG       "${PROM_HOST}/api/v1/query"       --data-urlencode       'query=container_memory_working_set_bytes{namespace="cloud-exp",container="app"}'

echo
echo
echo "=== Prometheus CPU query ==="
curl -sG       "${PROM_HOST}/api/v1/query"       --data-urlencode       'query=sum(rate(container_cpu_usage_seconds_total{namespace="cloud-exp",container="app"}[1m]))'

echo
