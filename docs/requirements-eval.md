# Evaluation — "Entities Structure Review and Forward Planning" dashboard requirements

*Against the ir_dashboard / ddm-web architecture as of 2026-08-04 (post-v2
revision WS1–WS7 + close-readiness layer). Companion analysis:
`docs/ir-triage-categories.md` (2-year ir@ corpus).*

## Verdict

Roughly **70% of what the meeting asked for already exists** — the "rolling
dashboard" the team described is structurally the request tracker that shipped
in the v2 revision, pointed at investor-servicing traffic instead of the
SynthBee prospect book. The genuinely new work is concentrated in one
structural gap (intake scope, below) and a handful of small features. Nothing
in the meeting contradicts the architecture; two of its decisions
independently validate choices already made.

## Requirement → status map

| Meeting requirement | Status | Where |
|---|---|---|
| Trackable inquiry with lifecycle, owner, open/closed | ✅ exists | `new_inforequest`: status (New/InProgress/Waiting*/Completed/Cancelled), `ownerid` (user-owned table), received/due/completed dates |
| Green / yellow / red with aging (~2–3 days) | ✅ exists | WS6 deterministic escalation at render: amber >2 business days open, red >5 or past deadline; thresholds env-configurable — matches the meeting's numbers almost exactly |
| "Answered" vs "answered to resolution" | ✅ exists | Outbound-reply flip auto-stamps Completed + completeddate (= "answered"); human status edit is the resolution judgment; `aigenerated`/`humanconfirmed` keep the two distinguishable |
| AI summary of thread + latest response + per-investor over time | ✅ exists | WS5 cached contact summaries (`new_aisummary`, watermark-gated, nightly + on-demand) |
| Category / type of inquiry | ✅ extended today | Category choice now includes CapitalCall, TaxDocs, AccountAdmin, LiquidityTransfer (DEV; PROD at next manual import) |
| AI auto-route / recommend assignment | ◐ partial | WS6 routing taxonomy exists (prospect-oriented 4-way); servicing analog = category → suggested-owner map, small addition. Recommend suggest-and-confirm, never silent auto-assign |
| Historical analysis of inquiry types | ✅ done | `docs/ir-triage-categories.md` — 2 years of ir@, 393 human threads classified |
| Monitor without asking people (Eric view) | ✅ exists | WS7 headline cards, Ball-in-Court / Open Requests views, exact count↔list reconciliation |
| Not native D365 Cases; filter events out | ✅ by design | Custom plain table was chosen in Phase 1 for exactly this reason; the Tier-0 admin-blast gate (added today) is the "filter events out" mechanism — 782 of 1,494 real ir@ inbound (52%) are fund-admin robots that now land as `admin_blast` noise, never tickets |
| Replies visible regardless of mailbox | ✅ exists | All 9 mailboxes sync Inbox **and** SentItems — a reply from a personal mailbox is captured automatically. This answers open decision #1: IR-mailbox-only is a **rapport/identity preference, not a tracking requirement** |
| No automated responses | ✅ by design | Only audited, confirm-click writes exist anywhere |
| Reopen / new ticket on fresh question | ✗ gap (small) | Sync currently creates a new signal but never reopens a Completed request in the same conversation. Small pass addition |
| Adequacy evaluation of the response | ✗ gap (small) | New LLM feature; same pattern as WS5/WS6 (cached, `aigenerated`), marked as opinion never authority |
| Event-spike anticipation / company line prep | ✗ gap (process+small) | Category taxonomy gives the event→question mapping (K-1 season → TaxDocs, Carta launch → SubscriptionDocs); a prepared-responses page per event is mostly editorial work |
| Michal's investor relationship spreadsheet | ✗ pending input | Small import to contact fields when the sheet lands; feeds triage context |
| Phone visibility, evenings | ⚠ check | ddm-web is browser-based; responsive behavior on phones needs a UAT pass. Data freshness is polling-bound (~15 min worst case) — fine vs. the bold/unbold status problem it replaces, but not push-instant |

## The one structural gap: intake scope

Today an `inforequest` row is born only when the classifier flags a
**contact-matched** inbound as a request. The meeting wants **every human
inquiry into ir@** visible with an answered-state. Two populations fall
through the current funnel:

1. **Non-request correspondence** — categorized mail that needs no action
   (is_request=false, ~43% of human threads). These exist as signals but have
   no ticket presence; the meeting's "see what came in and how it was
   answered" implies they should at least be visible in the IR view, if not
   ticketed.
2. **Unknown senders** — the 2-year analysis measured **39% of human ir@
   threads coming from third parties** (CPAs, advisors, custodians, auditors),
   many of whom are not CRM contacts. Today `no_contact` messages are dropped
   entirely — a CPA chasing a K-1 for an investor never reaches the dashboard.

Recommended fix (needs operator sign-off, not built yet): an ir@-specific
intake rule — every non-noise inbound to ir@ gets a signal regardless of
contact match (sender stored raw, contact linked when matched), and a ticket
when `is_request` or when no reply exists after N hours. This is the largest
remaining piece of meeting scope and it touches the matching pipeline, so it
should be its own reviewed change.

## Recommendations on the open decisions

1. **Must replies originate from ir@?** No — tracking works either way because
   all nine mailboxes are synced, and the latency/answered pass links replies
   by conversation. Keep "reply from IR when practical" as an
   identity/rapport rule; CC-ing IR remains belt-and-braces for external
   threads started elsewhere.
2. **Assignment model:** AI-suggested owner (category → default-owner map,
   overridable per item) + one-click self-assign/confirm. Silent auto-assign
   risks the exact "someone assumed someone else had it" failure the
   dashboard exists to kill.
3. **Resolution confirmation:** owner's judgment closes; the system already
   detects an investor replying again (conversation signal) — wire that as an
   automatic reopen instead of asking investors to confirm resolution.
   Objective, zero investor friction.
4. **Native Cases vs custom:** custom, as built. The meeting's own complaint
   (Cases auto-trigger on every inbound) is the reason the architecture
   avoided the activity/case model in Phase 1.
5. **Communicating the team model to investors:** business decision — the
   platform supports either; nothing blocks on it.

## Delivered with this evaluation (operator-approved items 1–3)

- **Classifier v2** (`ingestion/llm.py`, `classify.py`): returns primary +
  secondary category (13-value taxonomy), `is_request`, `third_party`,
  and a real confidence 0–100; rides the existing `CLASSIFIER_BACKEND`
  env flag; cache key bumped (`req2:`) so stale v1 verdicts don't leak.
- **Schema (DEV applied 2026-08-04):** `new_category` += CapitalCall /
  TaxDocs / AccountAdmin / LiquidityTransfer (values 100000009–12,
  append-only); new columns `new_secondarycategory`, `new_thirdparty`,
  `new_classifierconfidence` on `new_inforequest`. PROD receives these at the
  next manual maker-portal solution import; until then sync probes column
  existence and skips the new fields in PROD (same pattern as the §0.1
  close-readiness columns).
- **Tier-0 gate** (`rules.json` bulk_senders/bulk_sender_domains +
  `noise.py`): fund-admin and platform robot senders → `admin_blast` noise —
  recorded with reason, never classified, never a ticket. Named fund-admin
  staff (a person @apexgroup.com) deliberately not listed.
- Tests: 63 → 67 passing.

## Suggested next backlog (in priority order, each needing a go-ahead)

1. ir@ intake scope rule (the structural gap above)
2. Reopen-on-reply for Completed requests
3. Category→owner suggestion map + assignee UI in ddm-web
4. Response-adequacy evaluation (LLM, cached, opinion-only)
5. Event playbook page (prepared answers per event type)
6. Investor-relationship import when Michal's spreadsheet arrives
