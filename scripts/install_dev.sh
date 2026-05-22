#!/usr/bin/env bash
set -euo pipefail

echo "[dhn-client] Installing system dependencies..."
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y \
    python3.11 \
    python3.11-venv \
    git \
    build-essential \
    tcpdump \
    iproute2 \
    net-tools
else
  echo "apt-get not found. Install Python 3.11, git, build-essential, tcpdump, and iproute2 manually."
fi

echo "[dhn-client] Installing uv if missing..."
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "[dhn-client] Creating Python 3.11 virtual environment..."
uv venv --python 3.11 .venv

echo "[dhn-client] Activating venv..."
source .venv/bin/activate

echo "[dhn-stream] Installing package and dependencies..."
uv pip install --upgrade pip
uv pip install -e .
uv pip install \
  numpy \
  scipy \
  typer \
  rich \
  spikeinterface \
  dhn-med-py \
  pytest \
  ruff \
  mypy \
  pyyaml

echo "[dhn-client] Verifying imports..."
python - <<'PY'
import numpy
import spikeinterface
print("OK: numpy", numpy.__version__)
print("OK: spikeinterface imported")
PY

echo "[dhn-stream] Done."
echo "Activate with: source .venv/bin/activate"
