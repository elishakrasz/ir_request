#!/usr/bin/env bash
# Cron wrapper for the restricted IR-request route (Option B): reads only
# ir@/mravid@/lgruber@ (dedicated Access-Policy-locked app), CRM-contacts-only,
# spam-filtered, and is the ONLY route that opens Information Requests.
# Own lock + delta store, so it never collides with the broad engagement sync.
set -u
cd "$(dirname "$0")/.." || exit 1
LOG="ingestion/state/ir_request.log"

exec 9>ingestion/state/.ir_request.lock
flock -n 9 || exit 0

echo "=== $(date -u +%FT%TZ) ir-request start" >> "$LOG"
venv/bin/python -u -m ingestion.run_ir_request --apply --months 3 >> "$LOG" 2>&1
echo "=== $(date -u +%FT%TZ) exit $?" >> "$LOG"

if [ "$(stat -c%s "$LOG")" -gt 2000000 ]; then
  tail -c 1000000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
