#!/bin/zsh
# Double-click to run PhantomFrame on macOS (install.sh sets it up)
cd "${0:A:h}"
[ -x .venv/bin/python ] || PHANTOMFRAME_DIR="$PWD" exec bash ./install.sh
exec .venv/bin/python gta6_framegen.py "$@"
