#!/bin/bash
#SBATCH --job-name=InternetSim
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=80gb
#SBATCH --time=24:00:00
#SBATCH --partition=vhpc

set -euo pipefail

# Get the Absolute Path of the setup Script
SCRIPT_FILEPATH=$(realpath $0)

# Get the Absolute Path of the Project Directory
ROOT_DIR=$(dirname "$SCRIPT_FILEPATH")

VENV_DIR="${ROOT_DIR%/}/.venv"
source "${VENV_DIR%/}/bin/activate"

cd "$ROOT_DIR"

python3 Main.py "$@"
