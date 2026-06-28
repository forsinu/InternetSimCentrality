#!/bin/bash
#SBATCH --job-name=InternetCentrality
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64gb
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

MIN_SAMPLE_PATHS=10000
MAX_SAMPLE_PATHS=5776000000
SAMPLE_PATH_COUNTS=(
    10000
    25000
    50000
    100000
    250000
    500000
    1000000
    2500000
    5000000
    10000000
    25000000
    50000000
    100000000
    250000000
    500000000
    1000000000
    2000000000
    4000000000
    5000000000
)

for sample_paths in "${SAMPLE_PATH_COUNTS[@]}"; do
    if ! [[ "$sample_paths" =~ ^[0-9]+$ ]]; then
        echo "[-] Invalid sample path count: ${sample_paths}"
        exit 1
    fi
    if [[ "$sample_paths" -lt "$MIN_SAMPLE_PATHS" || "$sample_paths" -gt "$MAX_SAMPLE_PATHS" ]]; then
        echo "[-] Sample path count out of range: ${sample_paths}"
        exit 1
    fi

    echo "[+] Running centrality with ${sample_paths} sampled path(s)"
    python -m scripts.centrality "$@" --sample-paths "$sample_paths"
done
