#!/usr/bin/env bash
# Local reproduction harness for the CSV-append bug.
# Runs the repro against the real CDF using the test venv. Mac dev box.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEBAPP="$(cd "$HERE/../.." && pwd)"
VENV_PY="$HOME/.gc_consolidation_venv/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
  echo "Test venv python not found at $VENV_PY" >&2
  exit 1
fi

cd "$WEBAPP"
echo "Running CSV-append reproduction from: $WEBAPP"
"$VENV_PY" tests/manual/repro_csv_append.py
