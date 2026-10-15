#!/bin/sh
set -eu
python3 -m venv .venv
.venv/bin/python -m pip install -r .veritygate/python-lock.txt
.venv/bin/python -m pip install --no-deps -e .
npm ci --prefix .veritygate/toolchain --ignore-scripts --no-audit --no-fund
