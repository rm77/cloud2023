#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

"${SCRIPT_DIR}/build-load.sh"
"${SCRIPT_DIR}/deploy.sh"
"${SCRIPT_DIR}/verify.sh"
