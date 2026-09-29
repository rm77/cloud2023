#!/bin/sh
set -eu

NAMESPACE="${NAMESPACE:-cloud-exp}"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BASE_DIR=$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)

kubectl create namespace "${NAMESPACE}"       --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f "${BASE_DIR}/k8s/deployment.yaml"

kubectl rollout status deployment/experiment-app       -n "${NAMESPACE}"       --timeout=180s

kubectl get pods -n "${NAMESPACE}"
kubectl get svc -n "${NAMESPACE}"
kubectl get ingress -n "${NAMESPACE}"
