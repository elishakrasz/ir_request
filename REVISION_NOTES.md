# v2 Revision Notes

Working log for the v2 revision spec. Updated per workstream.

## Phase 0 — discovered schema mappings (probed PROD read-only, 2026-07-29)

| Spec concept | Actual schema |
|---|---|
| Signal table | `new_engagementsignal` (org-owned; see `docs/schema.md`) |
| Disposition `auto_confirmed` | `new_matchstatus=Confirmed` + `new_matchmethod≠Manual` (Manual = human-confirmed) |
| Disposition `needs_review` | `new_matchstatus ∈ {Suggested, Unmatched}` |
| Disposition `noise` | `new_matchstatus=Excluded` + **new column** `new_noisereason` (schema add, WS1) |
| Live flag | `new_live` (Boolean) — **128 of 2,141** open opps are live |
| Prospect code | `new_prospectcode` (String) — **126/128 live opps have one** (e.g. `EXG-1040`) |
| Pipeline type | `mint_opportunitypipelinetypes` (Picklist): "ECG Investor for Fund SPV" = 1,131 opps, blank = 893, "ECG Deal" = 90, EFO = 27 |
| Fund lookup | `mint_fundorspv` → `mint_investmentproduct` (unchanged from v1) |
| Request table | `new_inforequest` (exists, dormant) + **new columns** `new_statedurgency`, `new_explicitdeadline` (WS6) |
| Summary storage | **new columns on `contact`**: `new_aisummary`, `new_aisummaryat`, `new_aisummarywatermark` (WS5) |
| "Regarding" ground truth | `email._regardingobjectid_value`; join signals↔email activities on internetMessageId |

## Phase 0 — probe results

- **Regarding coverage**: of 1,930 email activities (30d), 20% carry any Regarding;
  **178 (~9%) regard an Opportunity** — usable as a confidence-100 booster, minority coverage.
- **Single-Opportunity fraction (live scope)**: 129 contacts hold live opps;
  **128 (99%) hold exactly one** (distribution {1: 128, 2: 1}). The spec's expectation that
  this auto-resolves most of the queue is confirmed decisively — v1's 60%-Suggested rate came
  from matching against ALL 2,141 open opps (avg 3.2/contact); scoping to live kills it.
- **Median-response diagnosis**: the data layer is NOT empty — last 30d has 902 outbound
  signals, **404 with computed latency**. The "— 0 replies" on the dashboard is a view-layer
  filter interaction (fund-cohort + keyhash dedupe); WS3 rebuilds pairing to the spec's
  semantics (inbound → earliest subsequent outbound) rather than patching the view.
- **Queue size**: 1,902 Suggested/Unmatched signals in the last 90 days repo-wide
  (the observed 264 was under narrower dashboard filters).

## Deviations from spec (with rationale)

1. **Two repos, not one**: ingestion/schema live in `ir_dashboard` (this repo); the web
   front-end is `Tokens/ddm-web` (`/engagement`, Next.js — not Python; the Python/Streamlit
   app is the secondary ops tool). Workstream commits land in the repo they touch.
2. **Teams signals**: v1 never ingested Teams (protected-API request never filed), so WS5's
   "exclude Teams" is vacuously satisfied; noted for when Phase 5 lands.
3. **Schema changes batched**: WS1/WS5/WS6 columns are provisioned to DEV in one pass and
   arrive in PROD via ONE manual maker-portal solution import (house rule: no CLI schema
   writes to PROD), scheduled at the start of WS1 rather than per-workstream.
4. **Blocklist storage**: config file (`ingestion/rules.json`) rather than a Dataverse table —
   spec allows either; config avoids an extra schema round-trip and the file is
   operator-editable without deploy (it ships with the sync host, not the web image).
5. **Matcher scope**: "live scope" implemented as `new_live=Yes` (the operator-maintained
   flag the buzz pipeline already flips), NOT v1's `new_activemonitoring` (which we had set
   on all 2,141 opps). `new_activemonitoring` is retired from the match path.

## WS1 — queue-backfill dry-run stats (PROD, 2026-07-29)

6,139 signals reprocessed through the v2 pipeline (noise gate → boosters →
thresholds). LLM triage: 800 heuristic-undecidable candidates classified by
claude-haiku-4-5 in 20 batched calls (79k in / 12k out tokens ≈ $0.14; verdicts
cached, so --apply re-uses them free).

| Transition | Rows |
|---|---|
| needs_review → auto_confirmed | 2,447 |
| auto_confirmed → auto_confirmed (untouched) | 2,412 |
| needs_review → noise | 1,170 |
| auto_confirmed → noise (newsletters that had been confirmed) | 66 |
| needs_review → needs_review (**the queue after backfill: 44**) | 44 |

**Open judgment call (blocking --apply):** 1,094 of the noise rows are
`low_confidence` — real correspondence with contacts whose own opportunity is
not `new_live=Yes` (top-candidate funds: HIPstr II 307, Trophy 179, CRB 126,
SynthBee 109…). Samples include capital-call reminders (e.g. HP Fund I-B), which
suggests some live flags may be incomplete in CRM. Reversible either way: noise
rows keep their data + reason, and a reclassify re-run after live-flag fixes
re-matches them automatically. Operator decides: apply as-is / fix flags first.

## WS2 — findings + deviation

**PROD data truth (2026-07-29): all 128 live opportunities belong to Exigent
SynthBee Holdings LP, and every live opp has a BLANK
`mint_opportunitypipelinetypes`.** Consequences:
1. The spec's fund-selector condition (live AND pipeline type = "ECG Investor
   for Fund SPV") selects **zero** funds. Deviation: selector/roster use
   live-only, accepting blank pipeline type (`isLiveInvestorOpp` in view.jsx);
   the type check re-engages automatically if CRM starts populating it.
2. The dashboard's active book is currently SynthBee-only — HIPstr/HP/FUS1/xAI
   investor correspondence sits in noise (low_confidence) until those opps are
   flagged live through the normal business process (⚠️ live flips trigger the
   Buzz delivery flow — never bulk-edit), after which `reclassify` re-runs
   resurrect it automatically.

## WS3 — response pairing (complete)

Rebuilt to spec semantics: pair = meaningful inbound → earliest subsequent
meaningful outbound, same conversation + same contact; latency in **business
minutes (Sun–Thu, Asia/Jerusalem)** stored on the INBOUND row. PROD migration
(`repair_latency`): 1,115 inbound latencies written, 2,043 stale v1 values
nulled — verified 1,125 inbound-anchored / 0 leftovers after the next sync
tick. **Median response: 8.7 business hours** (the "—, 0 replies" card is
fixed). Awaiting-reply = inbound with null latency older than 48 business
hours → headline card + oldest-first list. 11 pairing/bizhours unit tests
(cross-midnight, cross-weekend, multi-inbound, outbound-first, only-null).

## WS4 — honest health states (complete)

Four states with precedence overdue-red > gray "Never heard from" (no inbound
ever) > red >21d > amber (8–21d or awaiting-reply breach) > green, each with a
derived reason string; sorted red→amber→gray→green in the flat table and the
roster. First render surfaced 558 never-heard-from cohort contacts that v1
displayed as "Healthy".

## WS5 — cached AI summaries (complete)

`ingestion/summaries.py`: watermark-gated (no new confirmed signals → zero LLM
calls, verified by design + logs), 30d confirmed-email input truncated
oldest-first to 24k chars, claude-opus-5 with the spec's prompt contract and
exact sentinel; per-run token logging; 529-resilient (skip → watermark retries
next run). Nightly cron 03:00 (`run_summaries.sh`) + on-demand
`--contact <id>`. Summaries render in the contact drill-down and inline on
request-tracker rows. Required role addition (done): Write on Contact.

## WS6 — requests: classifier + urgency + escalation (complete)

Phase 3 enabled: `classify.py` → `llm.classify_request` (claude-opus-5,
structured outputs, cached by subject+body hash, only on enriched confirmed
inbound, never in dry runs). Creates `new_inforequest` with one-sentence
description as title, category, `new_statedurgency` (set once, never
re-graded) and `new_explicitdeadline` (parsed date; also drives duedate).
Seeded-email acceptance: 3/3 correct (explicit_deadline→2026-08-05,
urgent_language, none). Escalation is deterministic at render
(`requests_logic.py` + JS mirror): amber >2 business days open, red >5 or past
deadline; thresholds env-configurable. Answered-suggestion stays the existing
outbound-reply flip (→ WaitingExternal), close remains human.

## WS7 — headline cards (complete)

Four cards mapped to the core questions — Overdue for touch (+ never-heard
secondary), Awaiting reply, Open requests (red/amber breakdown), To triage —
each click-scrolls to the list it counts, and counts derive from the same
arrays the lists render (exact reconciliation). Volume + median response +
active contacts moved to a caption under the filters.

## Workstream status

- [x] Phase 0 — this document
- [x] WS1 **complete** — backfill applied 2026-07-29 (operator chose "apply as-is"
  for the 1,094 low-confidence rows): PROD now Confirmed 4,868 / **queue 44** /
  Excluded 1,227 (low_confidence 1,094 + llm 133). Queue verified newsletter-free;
  remaining items are genuine multi-live-fund ambiguity. Noise excluded from
  dashboard timeline/counts.
- [x] WS2 active-fund filter + roster (see WS2 findings above)
- [x] WS3 response pairing
- [x] WS4 health states
- [x] WS5 AI summaries
- [x] WS6 requests
- [x] WS7 headline cards

Final test count: 55 passing (ingestion). All LLM features carry cost-control
stories in code comments and honor them (heuristics-first, batching, caching,
watermarks, dry-run-never-invokes).

---

## Close-Readiness Analysis Layer (directive 2026-08-02)

Adds the computed-views layer over the existing pipeline; every view lands as
an export-workbook sheet (`ingestion/export_xlsx.py`) — no UI this phase.
All thresholds in ONE config block: `analysis.THRESHOLDS`.

### Prerequisite fixes (§0)
- **§0.1 dedup** — `analysis.mark_primary()`: canonical identity =
  `(opportunity, messagekeyhash)` (keyhash = sha256(internetMessageId), so it
  is already mailbox-independent; the alternate key guarantees once-per-contact).
  Primary = sender-matching contact first, then stable id order. Rollups (§5
  latency tail, volume) consume primary rows only. **Deviation:** the flag is
  computed at export time, NOT stored — a stored `new_isprimary` column +
  `new_routingcategory` are added to provision.py §10 and will reach PROD with
  the next manual solution import; until then the sidecar/computed forms are
  authoritative.
- **§0.2 promotion** — `ingestion/promote_requests.py` (idempotent on the
  Source Signal lookup; dry-run default). Found & fixed en route:
  `_create_rfi` referenced an undefined `deadline_iso` and never wrote
  `statedurgency` (it had never successfully run — hence 0 request rows);
  `add_business_days` was Mon–Fri while everything else is Sun–Thu (fixed,
  pinned by test); the Answered flip now stamps `completeddate` (reply ts)
  + status Completed instead of parking at WaitingExternal.

### Views (§1–§6)
`ingestion/analysis.py`; sheets: Ball In Court, Open Requests, Deal Clock,
Funnel (+stuck cohort w/ per-prospect explanation), Latency Tail (median/p90/
exceedances — mean deliberately banned), Hygiene (never_contacted, gone_quiet,
delivery_failure, suggested_closed_lost).
- 4-way routing taxonomy (§2.2) classified by one cached Opus call at
  promotion (`llm.classify_promotion`); stored in
  `state/request_routing.json` until the choice column exists (see above).
- Deal clock (§3): close date = `THRESHOLDS.close_dates` fund map
  (SynthBee 2026-08-15); tracker `statusSince` vs last meaningful signal.
- Closed-lost (§6): keyword prescreen (`DECLINE_RE`) on each contact's LAST
  inbound only, then cached `llm.classify_closed_lost` — LLM sees only
  prescreened candidates (cost story).

Test count: 55 → 63 (rfi lifecycle ×3, analysis views ×5).
