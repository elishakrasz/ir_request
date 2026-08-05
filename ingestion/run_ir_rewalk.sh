#!/usr/bin/env bash
# One-time ir@ re-walk under the SAME lock as the 15-minute cron (no overlap):
# delta tokens for ir@ were archived to state/delta.ir-prewalk-20260805 and
# reset, so this re-reads the full ir@ history; INTAKE_FLOOR bounds contact
# auto-creation to the agreed window. Idempotent for already-known messages.
set -u
cd "$(dirname "$0")/.." || exit 1
LOG="ingestion/state/ir_rewalk_$(date -u +%Y%m%dT%H%M%SZ).log"

exec 9>ingestion/state/.sync.lock
flock -w 900 9 || { echo "could not obtain sync lock"; exit 1; }

venv/bin/python -u -m ingestion.sync --apply --mailbox ir@exigentcap.com \
  > "$LOG" 2>&1
echo "EXIT:$? LOG:$LOG"
tail -25 "$LOG"
