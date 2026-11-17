#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CALLER_NAMESPACE=${NAMESPACE-}
. "$ROOT/config/environment.env"
[ -n "$CALLER_NAMESPACE" ] && NAMESPACE=$CALLER_NAMESPACE

kubectl delete namespace "$NAMESPACE" --ignore-not-found
echo "Removed module namespace: $NAMESPACE"
