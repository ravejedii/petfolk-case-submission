#!/usr/bin/env bash
#
# Vercel build: make the deployed function able to run the REAL pipeline.
#
# Vercel's Node runtime ships no Python at all (verified: `python3` is ENOENT
# there), so the deploy carries its own interpreter. This script downloads a
# standalone CPython — the same 3.14 line the repo is developed on — installs
# pandas/numpy into ITS site-packages (so nothing needs PYTHONPATH at runtime),
# and builds the React app. api/index.js then spawns pybin/bin/python3 exactly
# the way `npm start` spawns .venv/bin/python locally.
#
# Nothing here touches the pipeline, the data, or the app's behaviour: the
# deployed server runs the same modules on the same CSVs as a local clone.

set -euo pipefail

PY_VERSION="3.14.7"
PY_RELEASE="20260814"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_RELEASE}/cpython-${PY_VERSION}+${PY_RELEASE}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"

echo "==> standalone CPython ${PY_VERSION} for the function"
curl -sSL -o /tmp/python.tar.gz "$PY_URL"
rm -rf pybin
tar xzf /tmp/python.tar.gz
mv python pybin
rm -f /tmp/python.tar.gz
./pybin/bin/python3 --version

echo "==> pipeline dependencies into the interpreter's own site-packages"
./pybin/bin/python3 -m pip install --quiet --no-cache-dir pandas numpy
./pybin/bin/python3 -c "import pandas, numpy; print('pandas', pandas.__version__, '/ numpy', numpy.__version__)"

echo "==> smoke test: the real pipeline imports under the bundled interpreter"
./pybin/bin/python3 -c "from pipeline import config, signals, verdicts; print('pipeline imports OK')"

echo "==> web app"
npm --prefix app ci
npm --prefix app run build

echo "==> build complete"
du -sh pybin app/web/dist
