#!/usr/bin/env bash
# WS5 nightly summaries batch (cron: 03:00). Watermark-gated — zero LLM calls
# when nothing changed. Log appended + bounded.
set -u
cd "$(dirname "$0")/.." || exit 1
LOG="ingestion/state/summaries.log"
exec 9>ingestion/state/.summaries.lock
flock -n 9 || exit 0
echo "=== $(date -u +%FT%TZ) summaries start" >> "$LOG"
venv/bin/python -m ingestion.summaries --apply >> "$LOG" 2>&1
echo "=== $(date -u +%FT%TZ) exit $?" >> "$LOG"
if [ "$(stat -c%s "$LOG")" -gt 2000000 ]; then
  tail -c 1000000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
