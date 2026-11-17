#!/bin/sh

# Install the official Metrics Server manifest and configure it to skip
# verification of kubelet serving-certificate CAs.
#
# For disposable/local clusters only. This does not disable TLS verification
# for all Kubernetes traffic; it affects Metrics Server -> kubelet connections.

set -eu

MANIFEST_URL="https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml"
NAMESPACE="kube-system"
DEPLOYMENT="metrics-server"
CONTAINER="metrics-server"
FLAG="--kubelet-insecure-tls"

for command in kubectl jq; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $command" >&2
    exit 1
  fi
done

if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "ERROR: kubectl cannot reach the current Kubernetes cluster." >&2
  exit 1
fi

echo "Installing Metrics Server from the official latest-release manifest..."
kubectl apply -f "$MANIFEST_URL"

echo "Adding $FLAG while preserving the other container arguments..."
CURRENT_ARGS="$(kubectl -n "$NAMESPACE" get deployment "$DEPLOYMENT" -o json \
  | jq -ce --arg name "$CONTAINER" \
      '[.spec.template.spec.containers[] | select(.name == $name) | (.args // [])][0]')"

UPDATED_ARGS="$(jq -cn --argjson args "$CURRENT_ARGS" --arg flag "$FLAG" \
  '$args + (if ($args | index($flag)) == null then [$flag] else [] end)')"

PATCH="$(jq -cn --argjson args "$UPDATED_ARGS" --arg name "$CONTAINER" \
  '{spec:{template:{spec:{containers:[{name:$name,args:$args}]}}}}')"

kubectl -n "$NAMESPACE" patch deployment "$DEPLOYMENT" \
  --type=strategic --patch "$PATCH"

echo "Waiting for the Metrics Server Deployment with the TLS flag..."
kubectl -n "$NAMESPACE" rollout status "deployment/$DEPLOYMENT" --timeout=180s

echo "Waiting for the Metrics API to become available..."
kubectl wait --for=condition=Available apiservice/v1beta1.metrics.k8s.io --timeout=180s

echo "Metrics Server is ready. Current node metrics:"
kubectl top nodes

#echo
#echo "For a local/test cluster, Metrics Server now skips kubelet certificate-CA verification."
#echo "For production, configure trusted kubelet certificates and remove $FLAG."


