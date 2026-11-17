#!/bin/sh
# Prepare this module's custom metrics in the cluster's Prometheus Adapter.
# Run from any directory: sh prepare/register-metrics.sh [adapter-release] [namespace]

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
CALLER_NAMESPACE=${NAMESPACE-}
. "$ROOT/config/environment.env"
[ -n "$CALLER_NAMESPACE" ] && NAMESPACE=$CALLER_NAMESPACE
VALUES_FILE="$SCRIPT_DIR/prometheus-adapter-values.json"
PROM_NAMESPACE="monitoring"
PROM_SERVICE="monitoring-kube-prometheus-prometheus"
ADAPTER_RELEASE=${1:-}
ADAPTER_NAMESPACE=${2:-monitoring}
DEFAULT_RELEASE="hpa-multi-metric-adapter"
CHART="prometheus-community/prometheus-adapter"

usage() {
    cat <<USAGE
Usage: sh prepare/register-metrics.sh [adapter-release] [namespace]

Checks shared metrics prerequisites, then installs the Prometheus Adapter if
none is Helm-managed, or merges this module's rules into the existing Helm
release. If multiple Adapter releases exist, specify the intended release and
namespace explicitly.

Requires: kubectl, helm, and jq.
USAGE
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi
if [ "$#" -gt 2 ]; then
    usage >&2
    exit 2
fi

for command in kubectl helm jq; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "ERROR: required command not found: $command" >&2
        exit 1
    fi
done

if [ ! -f "$VALUES_FILE" ]; then
    echo "ERROR: Adapter values file not found: $VALUES_FILE" >&2
    exit 1
fi
if ! kubectl cluster-info >/dev/null 2>&1; then
    echo "ERROR: kubectl cannot reach the current Kubernetes cluster." >&2
    exit 1
fi

echo "Checking Metrics Server..."
kubectl top nodes

echo "Checking Prometheus Service and EndpointSlices..."
kubectl get svc "$PROM_SERVICE" -n "$PROM_NAMESPACE" >/dev/null
ENDPOINT_COUNT=$(kubectl get endpointslice -n "$PROM_NAMESPACE" \
    -l "kubernetes.io/service-name=$PROM_SERVICE" -o json \
    | jq '[.items[].endpoints[]? | select(.conditions.ready != false) | .addresses[]?] | length')
if [ "$ENDPOINT_COUNT" -lt 1 ]; then
    echo "ERROR: Prometheus Service has no ready EndpointSlice addresses." >&2
    exit 1
fi

RELEASES=$(helm list -A -o json \
    | jq -r '.[] | select(.chart | startswith("prometheus-adapter-")) | [.name, .namespace, .chart] | @tsv')

if [ -n "$ADAPTER_RELEASE" ]; then
    if helm status "$ADAPTER_RELEASE" -n "$ADAPTER_NAMESPACE" >/dev/null 2>&1; then
        ADAPTER_INSTALLED=1
    else
        ADAPTER_INSTALLED=0
    fi
else
    RELEASE_COUNT=$(printf '%s\n' "$RELEASES" | awk 'NF { n++ } END { print n+0 }')
    case "$RELEASE_COUNT" in
        0)
            ADAPTER_RELEASE=$DEFAULT_RELEASE
            ADAPTER_INSTALLED=0
            ;;
        1)
            IFS="$(printf '\t')" read -r ADAPTER_RELEASE ADAPTER_NAMESPACE _ <<EOF_RELEASE
$RELEASES
EOF_RELEASE
            ADAPTER_INSTALLED=1
            ;;
        *)
            echo "ERROR: found multiple Prometheus Adapter Helm releases:" >&2
            printf '%s\n' "$RELEASES" >&2
            echo "Run this script with the intended release name and namespace." >&2
            exit 1
            ;;
    esac
fi

if [ "$ADAPTER_INSTALLED" -eq 0 ] && kubectl get apiservice v1beta1.custom.metrics.k8s.io >/dev/null 2>&1; then
    echo "ERROR: custom.metrics.k8s.io already exists, but no matching Helm release was selected." >&2
    echo "Specify the existing Adapter Helm release and namespace, or configure the non-Helm Adapter separately." >&2
    exit 1
fi

if ! helm repo list | awk '$1 == "prometheus-community" { found=1 } END { exit !found }'; then
    helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
fi
helm repo update prometheus-community

if [ "$ADAPTER_INSTALLED" -eq 0 ]; then
    echo "Installing Prometheus Adapter as Helm release '$ADAPTER_RELEASE' in '$ADAPTER_NAMESPACE'..."
    helm upgrade --install "$ADAPTER_RELEASE" "$CHART" \
        --namespace "$ADAPTER_NAMESPACE" \
        --create-namespace \
        --values "$VALUES_FILE"
else
    echo "Merging module metrics into Adapter release '$ADAPTER_RELEASE' in '$ADAPTER_NAMESPACE'..."
    CHART_NAME=$(helm list -n "$ADAPTER_NAMESPACE" -o json \
        | jq -r --arg release "$ADAPTER_RELEASE" \
            '.[] | select(.name == $release) | .chart')
    case "$CHART_NAME" in
        prometheus-adapter-*) CHART_VERSION=${CHART_NAME#prometheus-adapter-} ;;
        *)
            echo "ERROR: release '$ADAPTER_RELEASE' is not using the prometheus-adapter chart ($CHART_NAME)." >&2
            exit 1
            ;;
    esac

    TMP_DIR=$(mktemp -d)
    trap 'rm -rf "$TMP_DIR"' 0 HUP INT TERM
    MERGED_VALUES="$TMP_DIR/merged-values.json"
    helm get values "$ADAPTER_RELEASE" -n "$ADAPTER_NAMESPACE" -o yaml > "$MERGED_VALUES"
    if [ ! -s "$MERGED_VALUES" ]; then
        printf '{}\n' > "$MERGED_VALUES"
    fi

    jq -s '
      .[0] as $existing |
      .[1] as $module |
      $existing
      | .prometheus = (($existing.prometheus // {}) * ($module.prometheus // {}))
      | .rules.custom = (
          (($existing.rules.custom // []) + ($module.rules.custom // []))
          | unique_by([.name["as"] // "", .seriesQuery // ""])
        )
    ' "$MERGED_VALUES" "$VALUES_FILE" > "$TMP_DIR/merged-values.json"
    mv "$TMP_DIR/merged-values.json" "$MERGED_VALUES"

    helm upgrade "$ADAPTER_RELEASE" "$CHART" \
        --namespace "$ADAPTER_NAMESPACE" \
        --version "$CHART_VERSION" \
        --values "$MERGED_VALUES"
fi

echo "Waiting for the custom metrics API..."
kubectl wait --for=condition=Available \
    apiservice/v1beta1.custom.metrics.k8s.io \
    --timeout=180s

printf '\nPreparation complete. The module rules are registered in Prometheus Adapter.\n'
printf 'After deploying the app, verify its metrics with:\n'
printf '  kubectl get --raw "/apis/custom.metrics.k8s.io/v1beta1/namespaces/%s/pods/*/inflight_requests"\n' "$NAMESPACE"
printf '  kubectl get --raw "/apis/custom.metrics.k8s.io/v1beta1/namespaces/%s/pods/*/queue_depth"\n' "$NAMESPACE"
