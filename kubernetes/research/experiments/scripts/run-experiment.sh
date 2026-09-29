#!/bin/sh
set -eu

RUN_ID="${1:-}"
USERS="${2:-}"

if [ -z "$RUN_ID" ] || [ -z "$USERS" ]; then
  echo "Usage: $0 RUN_ID USERS"
  echo "Example: $0 run01 20"
  exit 1
fi

APP_HOST="${APP_HOST:-http://experiment.10.28.84.254.sslip.io}"
PROM_HOST="${PROM_HOST:-http://prometheus.10.28.84.254.sslip.io}"
NAMESPACE="${NAMESPACE:-cloud-exp}"
DEPLOYMENT="${DEPLOYMENT:-experiment-app}"
WARMUP_SECONDS="${WARMUP_SECONDS:-60}"
MEASUREMENT_SECONDS="${MEASUREMENT_SECONDS:-300}"
COOLDOWN_SECONDS="${COOLDOWN_SECONDS:-30}"
SPAWN_RATE="${SPAWN_RATE:-5}"
PROM_STEP="${PROM_STEP:-5}"

BASE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WORKLOAD_DIR="$BASE_DIR/workload"
SCRIPT_DIR="$BASE_DIR/scripts"
RAW_DIR="$BASE_DIR/data/raw/$RUN_ID"
PROCESSED_DIR="$BASE_DIR/data/processed"
META_FILE="$RAW_DIR/metadata.env"
EVENT_FILE="$RAW_DIR/events.csv"

mkdir -p "$RAW_DIR" "$PROCESSED_DIR"

now_epoch() { date -u +%s; }
now_iso() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
record_event() { echo "$(now_iso),$1,${2:-}" >> "$EVENT_FILE"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: missing command: $1"; exit 1; }; }

need kubectl
need curl
need locust
need python3

kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" >/dev/null
READY_REPLICAS=$(kubectl get deployment "$DEPLOYMENT" -n "$NAMESPACE" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)
READY_REPLICAS=${READY_REPLICAS:-0}
[ "$READY_REPLICAS" -ge 1 ] || { echo "ERROR: no ready replicas for $DEPLOYMENT"; exit 1; }

curl --silent --fail --max-time 10 "$APP_HOST/" >/dev/null || { echo "ERROR: app unreachable: $APP_HOST"; exit 1; }
curl --silent --fail --max-time 10 "$PROM_HOST/-/healthy" >/dev/null || { echo "ERROR: Prometheus unreachable: $PROM_HOST"; exit 1; }

echo "timestamp,event,value" > "$EVENT_FILE"
record_event RUN_START "$RUN_ID"

cat > "$META_FILE" <<META
RUN_ID=$RUN_ID
USERS=$USERS
APP_HOST=$APP_HOST
PROM_HOST=$PROM_HOST
NAMESPACE=$NAMESPACE
DEPLOYMENT=$DEPLOYMENT
WARMUP_SECONDS=$WARMUP_SECONDS
MEASUREMENT_SECONDS=$MEASUREMENT_SECONDS
COOLDOWN_SECONDS=$COOLDOWN_SECONDS
PROM_STEP=$PROM_STEP
INITIAL_READY_REPLICAS=$READY_REPLICAS
META

record_event WARMUP_START "$USERS"
locust -f "$WORKLOAD_DIR/locustfile.py" \
  --host "$APP_HOST" --headless -u "$USERS" -r "$SPAWN_RATE" \
  -t "${WARMUP_SECONDS}s" --only-summary > "$RAW_DIR/warmup.log" 2>&1
record_event WARMUP_END "$USERS"

START_EPOCH=$(now_epoch)
START_ISO=$(now_iso)
record_event MEASUREMENT_START "$USERS"

locust -f "$WORKLOAD_DIR/locustfile.py" \
  --host "$APP_HOST" --headless -u "$USERS" -r "$SPAWN_RATE" \
  -t "${MEASUREMENT_SECONDS}s" \
  --csv "$RAW_DIR/locust" --csv-full-history

END_EPOCH=$(now_epoch)
END_ISO=$(now_iso)
record_event MEASUREMENT_END "$USERS"

cat >> "$META_FILE" <<META
MEASUREMENT_START_EPOCH=$START_EPOCH
MEASUREMENT_END_EPOCH=$END_EPOCH
MEASUREMENT_START_ISO=$START_ISO
MEASUREMENT_END_ISO=$END_ISO
META

python3 "$SCRIPT_DIR/export-prometheus.py" \
  --prometheus "$PROM_HOST" \
  --namespace "$NAMESPACE" \
  --deployment "$DEPLOYMENT" \
  --start "$START_EPOCH" \
  --end "$END_EPOCH" \
  --step "$PROM_STEP" \
  --output "$RAW_DIR/prometheus.csv"

python3 "$SCRIPT_DIR/merge-data.py" \
  --run-id "$RUN_ID" \
  --workload-level "$USERS" \
  --locust "$RAW_DIR/locust_stats_history.csv" \
  --prometheus "$RAW_DIR/prometheus.csv" \
  --events "$EVENT_FILE" \
  --output "$PROCESSED_DIR/$RUN_ID.csv"

record_event COOLDOWN_START
sleep "$COOLDOWN_SECONDS"
record_event COOLDOWN_END
record_event RUN_END "$RUN_ID"

echo "Completed: $PROCESSED_DIR/$RUN_ID.csv"
