#!/bin/sh
set -eu

APP_HOST="${APP_HOST:-http://experiment.10.28.84.254.sslip.io}"
REQUESTS="${REQUESTS:-20}"

i=1
while [ "$i" -le "$REQUESTS" ]; do
  curl -s "${APP_HOST}/cpu" >/dev/null &
  i=$((i + 1))
done

wait
echo "Completed ${REQUESTS} concurrent CPU requests."
