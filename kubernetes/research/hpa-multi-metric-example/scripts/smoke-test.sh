#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CALLER_HOST_IP=${HOST_IP-}
. "$ROOT/config/environment.env"
[ -n "$CALLER_HOST_IP" ] && HOST_IP=$CALLER_HOST_IP

command -v curl >/dev/null 2>&1 || { echo "ERROR: curl is required" >&2; exit 1; }
URL="http://hpa-multi-metric-demo.$HOST_IP.sslip.io"
if [ "$INGRESS_HTTP_PORT" != 80 ]; then URL="$URL:$INGRESS_HTTP_PORT"; fi
echo "Checking $URL/healthz"
curl --fail --silent --show-error "$URL/healthz"
