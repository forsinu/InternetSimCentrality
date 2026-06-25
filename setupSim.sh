#!/bin/bash
set -euo pipefail

# Get the Absolute Path of the setup Script
SCRIPT_FILEPATH=$(realpath $0)

# Get the Absolute Path of the Project Directory
ROOT_DIR=$(dirname "$SCRIPT_FILEPATH")

VENV_DIR="${ROOT_DIR%/}/.venv"

if [[ ! -d "$VENV_DIR" ]]; then
    echo "[+] Trying to create Virtual Environment at $VENV_DIR"

    if python3 -m venv "$VENV_DIR"; then
        echo "[+] Virtual environment created successfully"
    else
        echo "[-] Error: could not create the virtual environment."
        echo "    You probably need to install Python venv support."
        exit 1
    fi
else
    echo "[+] Found an existing Virtual Environment at $VENV_DIR: no need to recreate"
fi

# Activate the virtual environment and install the required Python packages.
echo "[+] Activate Virtual Environment and install necessary dependencies"
source "${VENV_DIR%/}/bin/activate"

pip install --upgrade pip
pip install -r "${ROOT_DIR%/}/requirements.txt"

echo "[+] Compile and setup the simulator"

# Local installation prefix for the MongoDB C++ driver.
# This avoids installing files globally with sudo.
INSTALL_PREFIX="$HOME/.local"

# Create local installation directories if they do not exist.
mkdir -p \
    "$INSTALL_PREFIX/bin" \
    "$INSTALL_PREFIX/lib" \
    "$INSTALL_PREFIX/include"

# Make locally installed binaries, headers, and libraries visible during build.
export PATH="$INSTALL_PREFIX/bin:$PATH"
export LD_LIBRARY_PATH="$INSTALL_PREFIX/lib:${LD_LIBRARY_PATH:-}"
export PKG_CONFIG_PATH="$INSTALL_PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
export CPATH="$INSTALL_PREFIX/include:$INSTALL_PREFIX/include/mongocxx/v_noabi:$INSTALL_PREFIX/include/bsoncxx/v_noabi:${CPATH:-}"
export LIBRARY_PATH="$INSTALL_PREFIX/lib:${LIBRARY_PATH:-}"

# Temporary directory used only for downloading and building the driver.
TMP_DIR="$(mktemp -d)"

# Always remove the temporary directory when the script exits.
cleanup() {
    echo "[+] Removing temporary directory: $TMP_DIR"
    rm -rf "$TMP_DIR"
}

trap cleanup EXIT

# MongoDB C++ driver version and download URL.
DRIVER_VERSION="r3.10.1"
MONGOCXX_ARCHIVE_NAME="mongo-cxx-driver-${DRIVER_VERSION}.tar.gz"
MONGOCXX_URL="https://github.com/mongodb/mongo-cxx-driver/releases/download/${DRIVER_VERSION}/${MONGOCXX_ARCHIVE_NAME}"

# Download the MongoDB C++ driver archive.
echo "[+] Downloading MongoDB C++ driver $DRIVER_VERSION"
curl -L "$MONGOCXX_URL" -o "$TMP_DIR/$MONGOCXX_ARCHIVE_NAME"

cd "$TMP_DIR"

# Extract the downloaded archive.
echo "[+] Extracting archive"
tar -xzf "$MONGOCXX_ARCHIVE_NAME"

cd "mongo-cxx-driver-${DRIVER_VERSION}"

# Configure the CMake build.
# SSL, SASL, and AWS auth are disabled to keep the build simpler.
echo "[+] Configuring build"
cmake -S . -B build \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CXX_STANDARD=17 \
    -DCMAKE_INSTALL_PREFIX="$INSTALL_PREFIX" \
    -DCMAKE_PREFIX_PATH="$INSTALL_PREFIX" \

# Build the driver using all available CPU cores.
echo "[+] Building"
cmake --build build --parallel "$(nproc)"

# Install the driver into $HOME/.local.
echo "[+] Installing into $INSTALL_PREFIX"
cmake --install build

echo "[+] MongoDB C++ driver installed successfully"

# Return to the project root before compiling the Python/Cython extension.
cd "$ROOT_DIR"

# Compile the simulator extension in-place.
echo "[+] Building Python extension"
python setup.py build_ext --inplace

echo "[+] Setup completed successfully"