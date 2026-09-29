#!/bin/sh
# Dub Checker launcher for macOS and Linux.
# Finds Python 3.10+ (python.org framework, Homebrew, system, then PATH) and
# hands over to bootstrap.py, which creates .venv, installs packages and starts the app.
cd "$(dirname "$0")" || exit 1

is_ok() {
    [ -n "$1" ] && [ -x "$1" ] && "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

PY=""
candidates=""
for minor in 20 19 18 17 16 15 14 13 12 11 10; do  # python.org installers, newest first
    candidates="$candidates /Library/Frameworks/Python.framework/Versions/3.$minor/bin/python3"
done
candidates="$candidates /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3"
candidates="$candidates $(command -v python3 2>/dev/null) $(command -v python 2>/dev/null)"

for candidate in $candidates; do
    if is_ok "$candidate"; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "Dub Checker needs Python 3.10 or newer, and none was found."
    echo "macOS: install it from https://www.python.org/downloads/ (or: brew install python python-tk)"
    echo "Linux: install python3, python3-venv and python3-tk with your package manager."
    exit 1
fi

echo "Using Python: $PY"
exec "$PY" bootstrap.py "$@"
