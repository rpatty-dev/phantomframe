#!/bin/zsh
# Double-click to run PhantomFrame on macOS. Windows equivalent: Launch.bat.
cd "${0:A:h}"
exec ./run.command "$@"
