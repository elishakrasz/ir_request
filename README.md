# Investor Engagement Dashboard — ingestion & schema

Engagement monitoring and IR-desk triage for Exigent Capital Group, built on
Dynamics 365 / Dataverse. This repo holds the **Dataverse schema** and the
**Python ingestion service**; the working front-end is the `/ir-desk` and
`/engagement` pages of `Tokens/ddm-web` (Next.js). The Streamlit app under
`dashboard/` is the secondary ops tool.

What it does, end to end:

1. Graph delta-syncs nine mailboxes (Inbox + SentItems) every 15 minutes.
2. Every message becomes an **Engagement Signal** (`new_engagementsignal`):
   direction (Inbound / Outbound / Internal), matched contact, confidence-scored
   opportunity match, noise gate (auto-replies, bounces, admin blasts).
3. On the restricted **IR-request route** (ir@ and two personal mailboxes),
   actionable inbound is classified and opens an **Information Request**
   (`new_inforequest`) with AI *recommendations* — category, assignee, draft
   tier / draft reply, inferred status — each with provenance. A human promotes
   a recommendation to the actual field; nothing is ever sent automatically.
4. Response pairing, business-hours latency, health states, cached AI contact
   summaries and close-readiness views feed the desk and the export workbook.

Governing rules live in `CLAUDE.md` (house rules — DEV first, PROD only by
manual solution import, idempotent upserts, provenance, dry-run first, no full
bodies) and the IR-desk feature brief kept alongside `Tokens/` (§4.1–4.6).

## Layout

```
ingestion/            sync service (python -m ingestion.sync), matching, noise
                      gate, classifier, LLM helpers, latency, analysis views,
                      run_*.sh cron wrappers, tests/
solution/             provision.py (DEV schema, idempotent), export.sh, src/
dashboard/            Streamlit ops app (see dashboard/README.md)
docs/                 schema.md, prod-runbook.md, ir-intake-design.md,
                      ir-triage-categories.md, requirements-eval.md,
                      phase0/ (reference-table CSVs + loaders, backfills)
scripts/              one-off, read-only analysis / verification scripts
REVISION_NOTES.md     working log per workstream (what changed, why, numbers)
```

## Setup

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in — never committed
```

`.env` documents every variable. `DATAVERSE_URL` selects the environment;
`MAILBOXES` / `REQUEST_MAILBOXES` / `INTAKE_MAILBOXES` are explicit allowlists
(the code never enumerates users).

## Running

Dry run is the default everywhere; `--apply` writes.

```bash
venv/bin/python -m ingestion.sync                       # broad sync, dry run
venv/bin/python -m ingestion.sync --apply
venv/bin/python -m ingestion.run_ir_request --months 3  # IR-request route
venv/bin/python -m ingestion.summaries --contact <id>   # on-demand AI summary
venv/bin/python -m ingestion.export_xlsx                # full-dataset workbook
```

Scheduled (WSL crontab): `ingestion/run_sync.sh` every 15 min,
`ingestion/run_ir_request.sh` at :05/:20/:35/:50, `ingestion/run_summaries.sh`
nightly 03:00. Each wrapper is `flock`-guarded and runs the **working tree** —
an uncommitted edit under `ingestion/` is live at the next tick.

Run logs: `ingestion/state/runs/` and `ingestion/state/ir_request/runs/`
(one JSON per run: counts, samples, errors). Delta tokens: `ingestion/state/delta*/`.

## Schema changes

`solution/provision.py` is idempotent and refuses PROD. Apply to DEV with
`PROVISION_URL=<dev url> venv/bin/python solution/provision.py --apply`, export
with `solution/export.sh`, then import the zip into PROD by hand per
`docs/prod-runbook.md`. Code probes column existence (`has_attribute`) so it
keeps running in an environment that predates an import.

## Tests

```bash
venv/bin/python -m pytest -q        # fixture-driven, no live calls
```

Unit tests cover direction, matching, latency pairing, idempotent re-run, the
noise gate, intake, RFI lifecycle, assignee recommendation precedence, status
inference, and drafts. The Anthropic client is stubbed for every test.
