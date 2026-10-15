#!/bin/sh
set -eu
python3 -m venv .venv
.venv/bin/python -m pip install -r .code-quality-gate/python-lock.txt
.venv/bin/python -m pip install --no-deps -e .
npm ci --prefix .code-quality-gate/toolchain --ignore-scripts --no-audit --no-fund
node .code-quality-gate/toolchain/node_modules/code-quality-gate/src/cli.mjs setup
