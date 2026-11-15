#!/bin/bash
# wrss - aggregated RSS reader, port [REDACTED:loopback-port]
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
    echo "Creating venv (uses system site packages)…"
    python3 -m venv venv --system-site-packages
fi
source venv/bin/activate

export PYTHONUNBUFFERED=1
exec python3 wrss.py
