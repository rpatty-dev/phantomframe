#!/bin/zsh
# Double-click to run PhantomFrame on macOS
cd "${0:A:h}"
PY=$(command -v python3.12 || command -v python3)
[ -d .venv ] || { "$PY" -m venv .venv && .venv/bin/pip install -r requirements.txt; } || { rm -rf .venv; exit 1; }
exec .venv/bin/python gta6_framegen.py "$@"
