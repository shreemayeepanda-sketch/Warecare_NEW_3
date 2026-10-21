#!/bin/sh
# One-tap launcher for the Smart Hardware Diagnostic Scanner (Linux / macOS).
# Terminal:  chmod +x shds.sh && ./shds.sh   (double-click friendly on macOS
# if renamed to shds.command or opened via the Terminal "run in shell" option.)
#
# Any arguments are forwarded to the app, so the headless CLI works too:
#   ./shds.sh --scan --quiet --json report.json
set -u
cd "$(dirname "$0")" || exit 1

# Prefer pythonw (no console window); fall back to python3 / python.
# exec replaces this shell, so no lingering console stays attached.
if command -v pythonw >/dev/null 2>&1; then
    exec pythonw shds_gui.py "$@"
fi

if command -v python3 >/dev/null 2>&1; then
    exec python3 shds_gui.py "$@"
fi

if command -v python >/dev/null 2>&1; then
    exec python shds_gui.py "$@"
fi

echo "Python was not found on PATH." >&2
echo "Install Python 3.11+ from https://www.python.org/downloads/ and try again." >&2
exit 1
