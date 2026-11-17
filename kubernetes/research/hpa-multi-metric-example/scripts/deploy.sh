#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CALLER_HOST_IP=${HOST_IP-}
CALLER_NAMESPACE=${NAMESPACE-}
. "$ROOT/config/environment.env"
[ -n "$CALLER_HOST_IP" ] && HOST_IP=$CALLER_HOST_IP
[ -n "$CALLER_NAMESPACE" ] && NAMESPACE=$CALLER_NAMESPACE

command -v kubectl >/dev/null 2>&1 || { echo "ERROR: kubectl is required" >&2; exit 1; }
case "$HOST_IP" in *[!A-Za-z0-9.-]*|'') echo "ERROR: HOST_IP must contain an IP or DNS-safe host value" >&2; exit 2 ;; esac
case "$NAMESPACE" in *[!a-z0-9-]*|'') echo "ERROR: NAMESPACE must be a lowercase Kubernetes name" >&2; exit 2 ;; esac

sed "s/__NAMESPACE__/$NAMESPACE/g" "$ROOT/k8s/namespace.yaml" | kubectl apply -f -
sed -e "s/__NAMESPACE__/$NAMESPACE/g" "$ROOT/k8s/app-template.yaml" | kubectl apply -f -
sed -e "s/__NAMESPACE__/$NAMESPACE/g" "$ROOT/k8s/hpa-multi-metric-template.yaml" | kubectl apply -f -
sed -e "s/__NAMESPACE__/$NAMESPACE/g" -e "s/__HOST_IP__/$HOST_IP/g" \
  "$ROOT/k8s/ingress-template.yaml" | kubectl apply -f -

kubectl -n "$NAMESPACE" rollout status deployment/hpa-multi-metric-demo --timeout=180s
URL="http://hpa-multi-metric-demo.$HOST_IP.sslip.io"
if [ "$INGRESS_HTTP_PORT" != 80 ]; then URL="$URL:$INGRESS_HTTP_PORT"; fi
printf '\nApp URL  : %s/\nNamespace: %s\n' "$URL" "$NAMESPACE"
printf 'Check HPA: kubectl get hpa,pods -n %s -w\n' "$NAMESPACE"
