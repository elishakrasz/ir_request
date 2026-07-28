#!/usr/bin/env bash
# Background apply launcher — appends to a numbered log with a REAL exit code
# (invoking the pipeline directly through wsl.exe mangles $? expansion).
set -u
cd "$(dirname "$0")/.."
LOG="ingestion/state/apply_$(date -u +%Y%m%dT%H%M%SZ).log"
echo "log: $LOG"
venv/bin/python -u -m ingestion.sync --apply >> "$LOG" 2>&1
echo "EXIT:$?" >> "$LOG"
