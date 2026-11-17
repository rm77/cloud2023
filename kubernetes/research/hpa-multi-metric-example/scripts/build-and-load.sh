#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CALLER_CLUSTER_NAME=${CLUSTER_NAME-}
. "$ROOT/config/environment.env"
[ -n "$CALLER_CLUSTER_NAME" ] && CLUSTER_NAME=$CALLER_CLUSTER_NAME

command -v docker >/dev/null 2>&1 || { echo "ERROR: docker is required" >&2; exit 1; }
command -v kind >/dev/null 2>&1 || { echo "ERROR: kind is required to load the image" >&2; exit 1; }

IMAGE=hpa-multi-metric-demo:v1
docker build -t "$IMAGE" "$ROOT/app"
kind load docker-image "$IMAGE" --name "$CLUSTER_NAME"
echo "Loaded $IMAGE into kind cluster $CLUSTER_NAME"
