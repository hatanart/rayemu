#!/bin/sh
# Start the RAY video poker emulator (Linux, macOS, Raspberry Pi).
# The first start creates a Python environment in .venv and installs the
# needed packages (needs internet and a C compiler for machine68k).
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
    echo "First start: setting up Python packages in .venv ..."
    python3 -m venv .venv || { echo "python3 -m venv failed (on Debian/Ubuntu/Pi: sudo apt install python3-venv)"; exit 1; }
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -r requirements.txt || {
        echo "Package install failed. On Debian/Ubuntu/Pi try: sudo apt install build-essential python3-dev"
        rm -rf .venv
        exit 1
    }
fi
exec .venv/bin/python -m pyray "$@"
