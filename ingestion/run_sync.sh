#!/usr/bin/env bash
# 15-minute incremental sync (interim scheduler — cron/Task Scheduler).
# Overlap-guarded (flock): a slow run simply skips the next tick.
# Idempotent by design: missed ticks (laptop asleep) catch up on the next run.
set -u
cd "$(dirname "$0")/.." || exit 1
LOG="ingestion/state/sync.log"

exec 9>ingestion/state/.sync.lock
flock -n 9 || exit 0

echo "=== $(date -u +%FT%TZ) sync start" >> "$LOG"
venv/bin/python -u -m ingestion.sync --apply >> "$LOG" 2>&1
echo "=== $(date -u +%FT%TZ) exit $?" >> "$LOG"

# keep the rolling log bounded (~2 MB)
if [ "$(stat -c%s "$LOG")" -gt 2000000 ]; then
  tail -c 1000000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
