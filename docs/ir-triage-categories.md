# ir@exigentcap.com — Incoming-request triage categories

Derived from a read-only Graph pull of the full `ir@exigentcap.com` mailbox,
**2024-08-04 → 2026-08-04** (all folders, metadata + `bodyPreview` only, per
house rule 6). Raw data and working files live in `reports/` (gitignored, PII).

**Corpus:** 2,862 messages scanned → 1,619 inbound (external sender) →
125 auto-reply/bounce/bulk noise removed → **1,494 real inbound messages**, of
which **782 are admin-platform blasts** (Apex, portal, Carta, Backstop) and
**~712 are human correspondence**, deduping to **393 distinct threads**.
Bucket counts below are keyword-based and overlap (one email often spans two
categories); they size relative volume, not exact shares.

A structural note that shapes the whole design: the mailbox's 47 subfolders are
all **per-investor** (person/family-office names) — the team triages by *who*,
never by *request type*. Sender→contact matching is therefore the natural
primary axis, and these categories are the second axis.

---

## Tier 0 — Auto-file, not a request (~52% of real inbound)

Machine-generated distributions where ir@ is a recipient of record. They need
filing/awareness, never a reply. All from a small, stable sender set:
`exigentcap.ir@apexgroup.com`, `highpost.ir@apexgroup.com`,
`portal@mail.investors.tzurmanagement.com`, `exigent@carta.com`,
`dse_*@docusign.net`, `backstopsolutions.com`, bank wire-notification robots.

| Sub-type | Signal |
|---|---|
| Capital call notices + reminders | "Capital Call #N due …", "Nth Reminder —" |
| Financial / capital account statements | "Unaudited Financial Statements and Capital Account Statement(s)" |
| K-1 / tax document notifications | "… Schedule K-1" |
| Quarterly / investment reports | "Qn Quarterly Report", "Investment Report" |
| Portal notifications & OTP codes | "Apex Israel Document Notification", "Your code is #" |
| DocuSign / Carta workflow notices | "Completed: …", "Signature and submission for …" |
| Bank wire-transfer notifications | "Wire Transfer Notification" (busey, texascapitalbank, …) |

Triage rule: match on sender + subject template; file to the fund/opportunity;
**exclude from engagement KPIs** (`ismeaningful=false` is wrong here — they are
real fund events — but they are not *requests*; they should never open an RFI).

## Tier 1 — Human requests (the triage queue, 393 threads / 2 yrs)

Ordered by observed volume.

### 1. Capital-call payment traffic (~96 threads, the single biggest human bucket)
Replies to call notices and to the team's own reminders: "wire sent, please
confirm receipt", "missed this / was in spam, paying now", proof-of-payment
attachments, payment-routing questions after entity renames, disputes about
amounts or commitment size, complaints about small/unconsolidated calls, and
fee-structure pushback on management-fee calls ("I thought fees were deducted
at realization"). **Sub-split worth keeping:** (a) payment confirmation — ack
and reconcile; (b) payment question/dispute — needs a substantive reply and is
SLA-sensitive.

### 2. Tax documents — K-1s and friends (~42 threads, most seasonal + most SLA-sensitive)
"When will 2024 K-1s be available?", resend requests, missing federal vs state
K-1, wrong-percentage K-1 blocking a mortgage ("URGENT"), CPA firms (KPMG, MNP,
ML Management, Leshkowitz…) collecting on behalf of clients, W-8/W-9 handling.
Spikes June–August. Distinct from Tier-0 K-1 *notifications*: these are humans
chasing.

### 3. Valuation / share-count / performance questions (~41 threads)
"What is the current valuation?", xAI vs X vs SpaceX share math, "how many
shares will I receive?", carry/fee impact on share counts, tax-planning
valuations, IRA custodians requiring year-end valuations (Inspira, Midwest
Trust, Equity Trust). Surged around the xAI/X merger and SpaceX IPO events.

### 4. Subscription & onboarding support (~27 threads, bursty per offering)
The SynthBee/Carta wave: can't complete subscription docs, signature rejected,
W-8BEN confusion, lost Carta invitations, ID/KYC document submission, "send
account opening forms", inviting family members to view documents. Each new
offering generates a burst; these are time-critical (closing deadlines).

### 5. Statements & reporting requests (~26 threads)
"Please send the latest capital account statement", statements missing for an
entity, firewall-blocked links → resend as attachment, discrepancies between
statements and investor records, audited FS requests (incl. Spanish investors'
audit-support requests).

### 6. Portal access & contact administration (~24 threads)
Portal credentials/password resets, "grant access to my accountant/advisor",
add/remove emails on distribution lists, change of email address, address
changes, FATCA/CRS classification updates, ownership transfers between
entities. Much of it arrives via named Apex staff (esther.berman@,
khushboo.palande@) as working correspondence, not blasts.

### 7. Liquidity, redemptions & distributions (~21 threads)
"I want to sell my shares" / hold-vs-sell elections on fund closures (with bank
instructions in-thread — **high-risk: wire-fraud surface, verify out-of-band**),
"what's the plan for returning our funds?", secondary buy interest ("if anyone
is selling, I'm a buyer"), tracing distributions that don't reconcile.

### 8. Share transfers & brokerage instructions (~11 threads, event-driven)
Post-IPO SpaceX share mechanics: DTC instructions, receiving-firm/account
details, IRA-custodian-to-brokerage transfers, Israeli banks' securities
desks (Leumi, Poalim). Same wire-fraud caution as #7.

### 9. Fund status / general updates (~18 threads)
"Any update on the medical companies?", "how is this investment going?",
questions in reply to investor updates, "explain the fund structure/trust
transition". Often from investors who feel out of the loop — churn-risk signal.

### 10. Meetings & calls (~15 threads)
Zoom scheduling and rescheduling, intro calls with the new IR head, "let's
have a call Wednesday". Low effort, but no-response here is very visible.

### 11. Third-party professional requests (cross-cutting flag, not a category)
A large share of threads in every category come not from the investor but from
their **CPA, wealth advisor, family office, or IRA custodian** (Fielder, Sage
Mountain, Monarch, Fiducient, Abante, Inspira, Vantage, Pacific Premier…), plus
auditor confirmations (EY). Worth a boolean flag: authority/verification rules
differ, and custodian items (COP signatures, year-end valuations) carry hard
external deadlines.

### 12. Legitimacy / verification checks (small but do-not-drop)
"Is this DocuSign email legitimate?" — investors verifying before signing or
wiring. Fast response directly prevents both fraud losses and abandoned
signings.

## Tier 2 — Noise (drop from queue)

Auto-replies/OOO/bounces (114 in corpus), read receipts and calendar responses,
marketing/newsletters (real-estate spam, vendor sales, Microsoft), internal
test emails (`itexigent@`, mintgroup.net D365 tests), IRONSCALES banners
(strip the banner text before classification — it pollutes previews).

---

## Mapping to the `new_inforequest` category choice

Current choice values: Reporting / CapitalAccount / Valuation / KYC-AML /
SubscriptionDocs / Legal-SideLetter / Meeting / DataRoom / Other.

| Observed category | Fits today | Recommendation |
|---|---|---|
| Capital-call payment traffic | ✗ (nearest: CapitalAccount) | **Add `CapitalCall`** — it is the biggest human bucket and its SLA/workflow is unlike statements |
| Tax docs / K-1 | ✗ | **Add `TaxDocs`** — #2 bucket, strongly seasonal, always ends in "send a document" |
| Valuation / shares | Valuation ✓ | keep |
| Subscription & onboarding | SubscriptionDocs ✓ | keep |
| Statements & reporting | Reporting / CapitalAccount ✓ | keep both; CapitalAccount = the statement itself, Reporting = fund-level reports |
| Portal & contact admin | ✗ | **Add `AccountAdmin`** (access, contact data, ownership/classification changes) |
| Liquidity / redemptions / transfers | ✗ | **Add `Liquidity-Transfer`** — high-risk, needs its own queue and verification workflow |
| Fund status / general | Other | acceptable, or add `FundStatus` if volume persists |
| Meetings | Meeting ✓ | keep |
| DataRoom, Legal-SideLetter | — | almost absent in 2 yrs of ir@; keep for completeness |

Suggested additional flags on the request row (already partly in schema):
`thirdparty` (CPA/advisor/custodian acting for a contact) and reuse
`aigenerated`/`humanconfirmed` as designed. The classifier stub in
`ingestion/classify.py` should emit these categories once the choice list is
extended.

## Triage-relevant volume facts

- ~2 real human threads per business day on average — the queue is small; the
  cost of a missed thread is reputational, not volume.
- Volume is **event-driven, not steady**: K-1 season (Jun–Aug), capital-call
  weeks, offering launches (SynthBee Jul 2026), and corporate events (xAI/X
  merger, SpaceX IPO) each triple the baseline.
- Top human-sender population is dominated by ~40 recurring investors/advisors
  — folder names in the mailbox are a ready-made roster of who matters.
- 2025-01→2025-04 shows a real lull (5–24 msgs/mo) — likely the IR-personnel
  transition visible in the threads ("Limor is no longer with Exigent"); several
  threads in 2025 H2 are investors chasing things dropped in that gap.
