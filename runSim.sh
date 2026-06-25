#!/bin/bash
set -euo pipefail

# Get the Absolute Path of the setup Script
SCRIPT_FILEPATH=$(realpath $0)

# Get the Absolute Path of the Project Directory
ROOT_DIR=$(dirname "$SCRIPT_FILEPATH")

VENV_DIR="${ROOT_DIR%/}/.venv"
source "${VENV_DIR%/}/bin/activate"

cd "$ROOT_DIR"

python3 Main.py "$@"
