# Claude Code Build Instructions — Investor Engagement Dashboard (Exigent)

You are building an engagement-monitoring system for a ~10-person private equity firm running Dynamics 365 / Dataverse. It tracks email (and later Teams) correspondence with all Contacts connected to a D365 Opportunity, stores normalized "engagement signals" in Dataverse, and displays them in a **Python (Streamlit) dashboard** — Power BI is explicitly out of scope for now.

Work in phases. **Stop at the end of each phase and wait for human review before continuing.** Do not skip gates.

---

## 0. Non-negotiable house rules

1. **DEV first, PROD never by CLI.** All Dataverse schema work is built and validated in the DEV environment. PROD (`exigentcrmprod.crm4.dynamics.com`) is only ever touched by exporting a managed/unmanaged solution and importing it **by hand via the maker portal**. Never run `pac` writes, Web API writes, or deployments against PROD unless the operator explicitly says "commit to PROD" in that session. The project `.env` deliberately contains **no PROD URL**.
2. **Idempotent everything.** Every ingestion write is an upsert keyed on a natural message key. Re-running any job against the same data must produce zero duplicates and zero silent mutations.
3. **Explicit provenance.** Every row records which mailbox, which delta run, and which code version produced it.
4. **Dry-run before write.** Every job has a `--dry-run` mode that logs intended upserts (counts + sample rows) without writing. First execution against any environment is always a dry run reviewed by the operator.
5. **PAC CLI:** the known-good version on this machine is **1.43.6**. If a newer build fails with a broken `DotnetToolSettings.xml`, prepend `%USERPROFILE%\.dotnet\tools` to PATH or pin 1.43.6.
6. **Store metadata + short snippets only.** Never persist full email/Teams bodies to Dataverse. Snippet max 2,000 chars.

Ask the operator for these values before Phase 1 (do not guess):

| Placeholder | Meaning | Status |
|---|---|---|
| `{DEV_URL}` | DEV Dataverse environment URL | **still needed from operator** |
| `{PREFIX}` | Publisher prefix for new schema (existing env mixes `mint_` and `new_`; operator decides — default suggestion `new_`) | pending operator confirmation |
| `{TENANT_ID}`, `{CLIENT_ID}` | App registration for Graph + Dataverse | **resolved — reusing the existing prospect-pipeline app** (see `.env`); operator adds `Mail.Read` per Phase 0 checklist |
| `{ORG_DOMAINS}` | Internal email domains (for inbound/outbound classification) | suggested `exigentcap.com` — confirm |

**App-reuse note (operator decisions, 2026-07-28):** the existing Entra app used by `prospect_pipeline` (Dataverse + app-only Graph) is reused rather than creating a new registration, and **tenant-wide `Mail.Read` is accepted** (other workloads on the same app need it — Exchange RBAC scoping was set up but cannot restrict an Entra grant). Mailbox scope is therefore enforced by configuration only: the explicit `MAILBOXES` allowlist in `.env`; the code must never auto-enumerate users. Rotating this app's secret affects the prospect pipeline too.

---

## Phase 0 — Operator prerequisites (produce a checklist, do not automate)

Generate `docs/phase0-checklist.md` covering, in the operator's words to execute manually:

1. **Entra app registration:** reuse the existing app (see `.env`); add application permission `Mail.Read` (Graph). Dataverse `user_impersonation` not needed — instead create a **Dataverse application user** for the app in DEV with a custom security role: read on `contact`, `opportunity`, `connection`, `email`; create/read/write on the new signal table.
2. **Mailbox scoping — prefer Exchange Application RBAC** (resource-scoped role assignment limiting the app's `Mail.Read` to an administrative unit / scope containing only the mailboxes in scope). Microsoft now identifies `ApplicationAccessPolicy` as legacy; document it only as the fallback if App RBAC proves awkward. List the exact Exchange Online PowerShell commands for both paths.
3. Admin consent for the Graph permission.
4. Client secret: the existing app secret is reused from the shared `.env` (never committed; Key Vault later). Track its expiry — rotation affects both this project and `prospect_pipeline`.
5. Note for later: Teams `ChannelMessage.Read.All` is a **protected API** requiring a Microsoft access request; defer to Phase 5, but link the request form in the checklist.

**GATE: operator confirms checklist complete and supplies placeholder values.**

---

## Phase 1 — Dataverse solution (schema)

Build an unmanaged solution `EngagementDashboard` in `{DEV_URL}` using `pac` (auth: `pac auth create --url {DEV_URL}`). Prefer declarative solution source under `solution/` in the repo, built with `pac solution` tooling, so the whole thing is re-creatable.

### Table: `{PREFIX}engagementsignal` (display: Engagement Signal)

| Column (schema) | Type | Notes |
|---|---|---|
| `{PREFIX}name` | Text (primary) | Subject, truncated to 200 |
| `{PREFIX}opportunity` | Lookup → opportunity | Regarding deal |
| `{PREFIX}contact` | Lookup → contact | Matched contact |
| `{PREFIX}channel` | Choice: Email / Teams | |
| `{PREFIX}direction` | Choice: Inbound / Outbound / Internal | |
| `{PREFIX}timestamputc` | DateTime (behavior: **UTC**) | Message sent/received time; ingestion always writes UTC |
| `{PREFIX}sender` | Text 320 | SMTP address |
| `{PREFIX}participants` | Multiline 4000 | JSON array of addresses |
| `{PREFIX}snippet` | Multiline 2000 | Body preview only |
| `{PREFIX}conversationid` | Text 512 | Graph conversationId — index |
| `{PREFIX}messagekey` | Text 512 | internetMessageId (email) / message id (Teams) — informational |
| `{PREFIX}messagekeyhash` | Text 64 | SHA-256 hex of messagekey — the actual key column (raw internetMessageIds can exceed 255 chars, so truncation risks collisions) |
| `{PREFIX}isinforequest` | Yes/No | Set by classifier (Phase 3) |
| `{PREFIX}rfistatus` | Choice: NA / Open / Answered / Overdue | Default NA. **Overdue is reserved — nothing writes it today**; overdue-ness is derived in the app layer from the request's `duedate`. Do not add a writer for it |
| `{PREFIX}responselatencymin` | Whole number | Reply latency, computed |
| `{PREFIX}matchconfidence` | Whole number | 0–100, set by the matcher |
| `{PREFIX}matchmethod` | Choice: Explicit / Thread / ContactMatch / Content / Manual | How the opportunity was chosen |
| `{PREFIX}matchstatus` | Choice: Confirmed / Suggested / Unmatched / Excluded | Suggested + Unmatched feed the review queue |
| `{PREFIX}sourcelink` | Text 2000 | Graph `webLink` deep link to the original message — kept **instead of** the body |
| `{PREFIX}ismeaningful` | Yes/No | False for auto-replies, bounces, mass mail; all KPIs filter on true |
| `{PREFIX}provenance` | Text 512 | `mailbox|runid|codeversion` |
| `{PREFIX}modifiedbyhint` | Text 200 | Human identity behind dashboard-initiated writes (the service principal performs the write) |

**Alternate key** on (`{PREFIX}messagekeyhash`, `{PREFIX}contact`) — one row per message × matched contact; this is the idempotency key for upserts. Dataverse alternate keys sit on a SQL index capped at **900 bytes**; a 512-char text column is 1,024 bytes on its own, which is why the key uses the 64-char hash and never the raw `messagekey`.

### Rollup columns

On **contact**: `{PREFIX}lastinbounddate` (MAX timestamp where direction=Inbound), `{PREFIX}lastoutbounddate` (MAX where Outbound), `{PREFIX}openrficount` — pointed at the **inforequest** table (the authoritative record), COUNT where status ∉ {Completed, Cancelled}. Overdue-ness is *derived* in the app layer from `duedate`, never a status of its own on the request table — so overdue items can never drift out of the open count. The signal-level `rfistatus` is display-only denormalization and is never used for rollups. On **opportunity**: same three. Total 30-day activity is NOT a rollup (rollups can't do rolling windows) — compute 7/30-day counts in the app layer instead. Note in docs that Dataverse rollups recalc on a schedule (~hourly at best); the dashboard treats them as convenience fields, not real-time truth.

### Table: `{PREFIX}inforequest` (display: Information Request)

A separate table, not just a flag on the signal — requests have their own lifecycle. Trimmed field set: name/title (primary), opportunity lookup, contact lookup, source signal lookup, category (choice: Reporting / CapitalAccount / Valuation / KYC-AML / SubscriptionDocs / Legal-SideLetter / Meeting / DataRoom / Other), receiveddate, duedate, completeddate, status (choice: New / InProgress / WaitingInternal / WaitingExternal / Completed / Cancelled), owner (user lookup), firstresponseminutes, `aigenerated` Yes/No, `humanconfirmed` Yes/No, `modifiedbyhint` (text — human identity behind dashboard writes). The signal-level `isinforequest`/`rfistatus` columns remain as lightweight denormalization, but the request row is the working record the team acts on.

### Opportunity extension columns (same solution)

`{PREFIX}oppcode` (short unique code, e.g. `OPP-0042`, usable in email subjects and Teams messages for explicit matching), `{PREFIX}aliases` (multiline: fund names, project names, abbreviations — matching evidence), `{PREFIX}activemonitoring` (Yes/No), `{PREFIX}monitoringstartdate` (ignore mail sent before this — prevents historical-backfill noise), `{PREFIX}teamschannelid` (text, reserved for Phase 5).

### Decision for the operator: plain table vs. custom activity

A custom **activity** table would surface signals on the native D365 timeline alongside tracked emails, but adds activity-party overhead and the choice is effectively irreversible. Since the display surface is the Python app and explicitly tracked emails already appear on the timeline, default to a **plain table** — but ask the operator before building, in case native timeline visibility is a hard requirement.

**Deliverables:** solution source in repo, `pac` build/import script for DEV, `docs/schema.md`.
**GATE: operator reviews schema in DEV before Phase 2.**

---

## Phase 2 — Ingestion service (Python, Azure Functions-ready)

Repo layout:

```
ingestion/
  function_app.py        # timer trigger (every 15 min), thin wrapper
  sync.py                # orchestrates a run
  graph_client.py        # MSAL client-credentials, delta queries, retry/backoff, 429 handling
  dataverse_client.py    # Web API auth, batch upserts via alternate key, contact/opportunity resolvers
  matching.py            # participant → contact matching
  classify.py            # RFI classifier interface + stub
  latency.py             # response-latency computation
  state/                 # delta tokens (local JSON in dev; Azure Table/Blob in prod)
  tests/
dashboard/               # Phase 4
solution/                # Phase 1
docs/
.env.example
```

Runs locally via `python -m ingestion.sync --dry-run` first; Azure Functions deployment is a later operator step, so keep `function_app.py` a thin adapter around `sync.py`.

### Logic

1. **Scope resolution (Dataverse):** fetch live Opportunities (operator-configurable filter; default: `statecode = Open`), then their Contacts via: (a) `parentcontactid`/customer on the opportunity, (b) `connection` rows linking opportunity ↔ contact (any role; make roles configurable). Build `{email → (contactid, [opportunityids])}` from `emailaddress1/2/3`, lowercased.
2. **Mailbox enumeration:** explicit list in config (`MAILBOXES=` in `.env`) — do not auto-enumerate all users. ~10 mailboxes.
3. **Delta sync per mailbox:** Graph delta on **Inbox and SentItems** folders (`/users/{id}/mailFolders/{folder}/messages/delta`), `$select` only: id, internetMessageId, conversationId, subject, bodyPreview, from, toRecipients, ccRecipients, bccRecipients, sentDateTime, receivedDateTime, **webLink** (`bccRecipients` matters on SentItems: outbound mail whose only external recipients are BCC'd would otherwise misclassify as Internal). NOTE: `internetMessageHeaders` is NOT returned on delta/list queries — header-based checks happen in the enrichment step (6) below. Persist delta tokens per mailbox+folder in `state/`. Handle token expiry (`resyncRequired`) by full re-sync — safe because upserts are idempotent. Ignore `@removed` entries — a later-deleted message doesn't un-happen as engagement, so the signal stays. Document in `docs/limitations.md` that messages moved out of Inbox before a sync cycle are missed: an accepted, explicitly stated gap at 15-minute polling.
4. **Direction (three-way — email gets Internal too):** sender domain ∉ `{ORG_DOMAINS}` → **Inbound**. Sender internal AND at least one external recipient (to/cc/bcc) → **Outbound**. Sender internal AND all recipients internal → **Internal** — a colleague's mail landing in a synced Inbox must never register as Outbound, or an internal FYI looks like a reply to the investor and poisons response-latency. Internal email signals are excluded from latency computation and from all external-engagement KPIs.
5. **Matching (hierarchical, confidence-scored):** first apply config-driven **exclusion rules** (excluded senders/domains/keywords — newsletters, HR/payroll, legal-privilege markers, named mailboxes) → write nothing or `matchstatus=Excluded` per config; flag likely auto-replies/bounces/mass mail from delta-level evidence (subject prefixes such as "Automatic reply:"/"Undeliverable:", sender patterns such as `no-reply@`, `mailer-daemon@`) → preliminary `ismeaningful=false`, finalized against real headers in the enrichment step (6). Then, for each matched contact, resolve the regarding opportunity in strict order, always recording `matchmethod` + `matchconfidence`:
   1. **Explicit** — `{PREFIX}oppcode` found in subject/snippet → confidence 95–100, `Confirmed`.
   2. **Thread inheritance** — the conversationId already has a Confirmed signal → inherit its opportunity, confidence 90, `Confirmed`.
   3. **Contact match** — contact maps to exactly one live, actively-monitored opportunity and the message postdates its `monitoringstartdate` → confidence 75, `Confirmed`.
   4. **Content** — an opportunity alias/fund name in subject/snippet narrows multiple candidates to one → confidence 60, `Suggested`.
   5. Otherwise → `Suggested` (top candidate recorded) or `Unmatched`. **Never silently assign at low confidence** — these rows land in the dashboard review queue; a human assignment is written back as `Confirmed`/`Manual`, and subsequent thread messages then inherit it via rule 2.
6. **Enrichment GET (matched messages only):** for each newly matched, non-excluded message, issue one single-message GET with `$select=internetMessageHeaders,uniqueBody,webLink` and header `Prefer: outlook.body-content-type="text"` (so `uniqueBody` comes back as plain text, not HTML). Use headers (`Auto-Submitted`, `Precedence: bulk/list`, `X-Autoreply`) to finalize `ismeaningful`; pass up to ~4,000 chars of body text to the classifier **in memory only**, persisting at most the 2,000-char snippet (house rule 6 still holds — this exists because `bodyPreview` is ~255 chars, far too little for reliable RFI classification). Matched messages are a small fraction of total mail, so the extra GETs are negligible at this volume.
7. **Upsert** to Dataverse via alternate key, `$batch` in chunks of ≤100. Never update rows whose incoming payload is identical (skip-if-unchanged to honor no-silent-mutation).
8. **Latency pass:** after upserts, for each touched conversationId compute reply latencies pairing **Inbound ↔ Outbound only — Internal signals are skipped entirely**: for an Outbound message, latency = minutes since the latest earlier Inbound in the same conversation (and vice versa); patch `responselatencymin` only where currently null.
9. **RFI classifier:** `classify.py` exposes `def classify(subject, text) -> RfiResult` (is_info_request, category, confidence), where `text` is the transient enriched body from step 6. Ship a stub returning `is_info_request=False`; design the interface so an LLM backend (Azure OpenAI or local Ollama endpoint) drops in behind an env flag. Every classifier-produced field is stamped `aigenerated=true` with its confidence, so human corrections (`humanconfirmed=true`) are always distinguishable — generated values never masquerade as the authoritative record. On `is_info_request=True` inbound → create an **Information Request** row (status New, category from classifier, owner unassigned) and set signal rfistatus=Open; a later Outbound reply in the same conversation flips the signal to Answered and the request to WaitingExternal or Completed per config; overdue-ness is derived from `duedate` (default: received + 2 business days when the classifier extracts no explicit deadline).
10. **Run log:** every run writes a JSON summary (run id, per-mailbox counts, matched/unmatched, upserted/skipped, ambiguities, errors) to `state/runs/`.

**Tests:** unit tests for direction, matching (multi-address contacts, case, plus-addressing left unmatched), latency threading, idempotent re-run (same input → zero writes). Use fixture JSON, no live calls.

**GATE: operator reviews a DEV dry-run log, then a real DEV run, before Phase 4.**

---

## Phase 3 — intentionally empty, no gate

The classifier stub ships inside Phase 2; wiring a real LLM backend is a separate later approval. The number is kept so phase references stay stable.

---

## Phase 4 — Streamlit dashboard

`dashboard/app.py`, reading **from Dataverse Web API** (same app registration; read-only usage), `st.cache_data` with ~5-min TTL. Plotly for charts. No writes from the dashboard except two audited, confirm-click actions: updating an Information Request's status/owner, and confirming a Suggested/Unmatched signal's opportunity (`matchstatus=Confirmed`, `matchmethod=Manual`). Because these writes go through the app registration, Dataverse audit shows the service principal — so the app must capture the human's identity into `modifiedbyhint` (Easy Auth header once deployed; a simple user picker when running locally). Also note in the UI/README: the `sourcelink` deep link opens the message **in the mailbox it was synced from**, so only that mailbox's owner can open any given link — expected behavior, not a bug.

Layout (one main per-opportunity page plus two auxiliary pages — review queue and firm overview):

1. **Sidebar:** Opportunity selector (live opps, searchable); date-range filter; channel/direction filters.
2. **KPI row (st.metric × 4):** % of connected Contacts engaged in last 30 days; open + overdue RFI counts; median our-response-time (30d); days since last activity on the deal.
3. **Contact health table** (main working queue): contact, role, last inbound, last outbound, days-since-last-touch, 30d message count, open RFIs, RAG chip (Green ≤7 days since touch, Amber 8–21, Red >21 or any Overdue RFI — thresholds in config). Sorted stalest-first. Row click → drill-down.
4. **Timeline:** interleaved signals for the opportunity, newest first — icon for channel, arrow for direction, subject + snippet expander, RFI badge.
5. **Contact drill-down (expander or second column):** response-latency sparkline (both directions), per-conversation thread list, open RFIs with age.
6. **Alerts strip (top, only when non-empty):** overdue RFIs; contacts silent > threshold; never-contacted contacts on the selected deal; count of items in the review queue.
7. **Review queue page:** all Suggested/Unmatched signals with the top candidate opportunity (and runner-up where available), sender, snippet, source link — one-click assign or exclude. Manual assignments teach the matcher via thread inheritance.
8. **Firm overview page:** cross-opportunity risk table — open/overdue requests, unanswered inbound count, days since last meaningful contact, review-queue count per deal — sorted worst-first. This stands in for a management Power BI dashboard for now.

Keep 7/30-day computations in pandas on the queried signal rows (don't rely on rollups for windows), and compute **all engagement KPIs over `ismeaningful=true`, non-Excluded signals only** — newsletters and auto-replies must never count as engagement. **Opportunity-level counts must dedupe on `messagekeyhash`**: one message matched to three contacts is three rows but one communication; contact-level metrics use the per-contact rows as-is. Include `dashboard/README.md` with run instructions (`streamlit run dashboard/app.py`) and a note that auth hardening (App Service + Easy Auth) is a deployment-time task.

**GATE: operator UAT on DEV data.**

---

## Phase 5 — Teams ingestion (design doc only, no code yet)

Write `docs/teams-phase.md`: channel-scoped ingestion via `ChannelMessage.Read.All` + `getAllMessages` on designated deal channels; note it is a protected API needing a Microsoft access request; **verify current licensing/metering status at build time** (as of mid-2026 metering was removed from these APIs in Aug 2025, but Teams API licensing has churned repeatedly and this detail goes stale); matching = contact name/email mentions in message text; direction = Internal. Stop there — no implementation without explicit go-ahead.

---

## Acceptance criteria (whole project)

- Re-running ingestion twice in a row produces zero new/changed Dataverse rows the second time.
- Every signal row has provenance populated.
- A message matched to N contacts counts exactly once in opportunity-level KPIs, and internal-only mail contributes to neither direction of response latency (both covered by unit tests).
- Dashboard loads the largest live opportunity in < 5 s on cached data.
- No secrets in the repo; `.env.example` documents every variable.
- PROD untouched except operator-driven manual solution import.
