#!/bin/bash
#SBATCH --job-name=Centrality
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=80gb
#SBATCH --time=24:00:00
#SBATCH --partition=vhpc
#SBATCH --output=centrality-%j.out
#SBATCH --error=centrality-%j.err

set -euo pipefail

# Get the absolute path of this script and the project directory.
SCRIPT_FILEPATH=$(realpath "$0")
ROOT_DIR=$(dirname "$SCRIPT_FILEPATH")

VENV_DIR="${VENV_DIR:-${ROOT_DIR%/}/.venv}"
if [[ ! -f "${VENV_DIR%/}/bin/activate" ]]; then
    echo "[-] Virtual environment not found at ${VENV_DIR}"
    echo "    Run ./setupSim.sh first, or set VENV_DIR to the correct environment."
    exit 1
fi

source "${VENV_DIR%/}/bin/activate"

cd "$ROOT_DIR"

python -m scripts.centrality "$@"
