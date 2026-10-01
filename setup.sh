#!/bin/bash
# One-time setup: the trio-test-harness submodule (unless TKSTRESS_HARNESS_DIR points at a checkout) and a virtualenv
# with both projects' Python dependencies. Python 3.12+ (trio-test-harness needs it).
set -euo pipefail
cd "$(dirname "$0")"
harness="${TKSTRESS_HARNESS_DIR:-trio-test-harness}"
if [ -z "${TKSTRESS_HARNESS_DIR:-}" ]; then
  git submodule update --init trio-test-harness
fi
if command -v uv >/dev/null; then
  uv venv --quiet --python ">=3.12" .venv
  uv pip install --quiet --python .venv/bin/python -r requirements.txt -r "$harness/requirements.txt"
else
  python3 -m venv .venv
  .venv/bin/pip install --quiet -r requirements.txt -r "$harness/requirements.txt"
fi
echo "ready: ./tandemkit-stress doctor"
