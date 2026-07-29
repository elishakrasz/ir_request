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

## Workstream status

- [x] Phase 0 — this document
- [x] WS1 **complete** — backfill applied 2026-07-29 (operator chose "apply as-is"
  for the 1,094 low-confidence rows): PROD now Confirmed 4,868 / **queue 44** /
  Excluded 1,227 (low_confidence 1,094 + llm 133). Queue verified newsletter-free;
  remaining items are genuine multi-live-fund ambiguity. Noise excluded from
  dashboard timeline/counts.
- [ ] WS2 active-fund filter + roster
- [ ] WS3 response pairing
- [ ] WS4 health states
- [ ] WS5 AI summaries
- [ ] WS6 requests
- [ ] WS7 headline cards
