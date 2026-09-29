#!/bin/sh
set -eu

IMAGE="${IMAGE:-experiment-app:v1}"
KIND_CLUSTER="${KIND_CLUSTER:-kind}"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BASE_DIR=$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)

echo "Building ${IMAGE}..."
docker build -t "${IMAGE}" "${BASE_DIR}"

echo "Loading ${IMAGE} into kind cluster ${KIND_CLUSTER}..."
kind load docker-image "${IMAGE}" --name "${KIND_CLUSTER}"

echo "Done."
