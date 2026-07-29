# PROD promotion runbook

Run this when the operator explicitly says **"commit to PROD"** — house rule 1:
PROD schema arrives ONLY via manual maker-portal solution import, never CLI.

## Pre-flight (operator)

- [ ] **CRM hygiene:** the contact "Carta" has email `no-reply@carta.com` — a robot
      matched as an investor. Fix or delete the contact in PROD (the ingestion
      exclusion rule for that sender is already in `rules.json` as a backstop).
- [ ] Confirm the PROD mailbox list — currently 8 in `.env`; decide whether
      `emallard@` (or others) should be added for full-firm coverage.
- [ ] Decide `INGEST_FLOOR` for PROD (DEV used 2026-01-01).
- [ ] Fresh export from DEV if schema changed since the last one:
      `solution/export.sh` → `solution/build/EngagementDashboard_managed.zip`.

## Promotion steps

1. **Manual import** of the managed zip via make.powerapps.com → PROD →
   Solutions → Import (rollup columns are included in the zip — no manual
   rebuild needed).
2. PROD **application user**: the shared app is already an app user in PROD
   (prospect pipeline). Create the minimal **Engagement Ingestion** role there
   (read contact/opportunity/connection/email; CRUD+append on the two new
   tables; append-to contact/opportunity/systemuser) and assign it.
   **No System Customizer in PROD** — schema never changes there by CLI.
3. Operator says "commit to PROD" → update `DATAVERSE_URL` in `.env`
   (and remove the provision-guard expectation: `provision.py` still refuses
   PROD — that stays true; only `sync`/`set_monitoring` run against PROD).
   ⚠ `sync`/`set_monitoring`/`progress` have no PROD guard by design at this
   point — the guard is this runbook + the explicit operator instruction.
4. `set_monitoring --fund … --start …` (dry → apply) for the funds to track.
5. `python -m ingestion.sync` (dry run) → operator reviews counts/breakdown.
6. `bash ingestion/run_apply.sh` — expect ~1 h (parallel walk ~35 min bounded
   by the largest folder + creates + latency pass).
7. Verify: `python -m ingestion.progress`, `python -m ingestion.verify_scope`,
   spot-check signals on a known opportunity, alternate-key status Active.

## Post-promotion

- [ ] Schedule the 15-min incremental sync (Azure Functions deploy, or a local
      cron/Task Scheduler interim).
- [ ] Dashboard: repoint reads at PROD (same `.env`), then begin the real UI
      build (Northbridge-style screen; likely a ddm-web page) — deferred by
      operator decision 2026-07-28.
- [ ] Phase 3 decision: real RFI classifier (lights up request tracker / SLA /
      priority queue panels).
- [ ] Review-queue triage session (~20 min) to seed thread inheritance.
- [ ] Secret rotation calendar note — shared app secret affects prospect
      pipeline too.
