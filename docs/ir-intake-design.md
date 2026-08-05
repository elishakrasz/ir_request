# ir@ intake scope — design note

*Decision record, 2026-08-05. Operator selected option 1 (auto-create
lightweight contacts). Context: `docs/requirements-eval.md` — 39% of human ir@
threads come from third parties (CPAs, custodians, advisors), many not in CRM;
today those messages die at `no_contact` before reaching the dashboard.*

## Goal

Every non-noise human email into the IR mailbox becomes a signal (and, when
actionable, a ticket) — regardless of whether the sender is a CRM contact —
without disturbing the engagement pipeline for the other eight mailboxes.

## Decision: auto-create lightweight contacts (option 1)

When an Inbound message arrives **in an intake mailbox** (config
`INTAKE_MAILBOXES`, initially `ir@exigentcap.com` only) and no participant
matches a scoped contact:

1. **Noise gate first.** The existing WS1 path decides: heuristics, then the
   Haiku triage for undecidables. Only `investor_correspondence` proceeds —
   newsletters, robots, and admin blasts never create contacts.
2. **Reuse before create.** Query Dataverse for ANY contact holding the sender
   address (emailaddress1/2/3) — the scope map only covers opportunity-linked
   contacts, so a CRM contact outside scope must be found and reused, never
   duplicated.
3. **Create if absent**: firstname/lastname parsed from the Graph display
   name, `emailaddress1` = sender, and `new_autocreatedby` =
   `ir-intake|<runid>` — the marker that distinguishes auto-created rows for
   review/cleanup (analogous to `aigenerated` on requests). One contact per
   sender per run cache; idempotent across runs via the reuse query.
4. **Signal lands as `Unmatched`** (review queue), never `low_confidence`
   noise — an intake-origin sender has no opportunities, and the meeting's
   requirement is that the item be *visible*. A human assignment then teaches
   the matcher via the normal thread-inheritance path.
5. **Classifier v2 runs as usual** on the enriched body; `is_request` creates
   the `new_inforequest` ticket (with `third_party` — expected true for much
   of this traffic).

## Why not the alternatives

- **Holding "IR Inbox" contact**: keeps the CRM clean but makes every third
  party an anonymous blob — no per-sender history, no aisummary, no linking a
  CPA to their investor. Rejected.
- **Relaxing the alternate key** `(messagekeyhash, contact)`: touches the
  idempotency guarantee every upsert depends on; highest blast radius for the
  least product value. Rejected.

Option 1 also matches the existing hub behavior (contact auto-creation from
name+email already exists in the onboarding flow), so the CRM pattern is
established, not novel.

## Containment / guardrails

- **Mailbox-scoped**: only mailboxes listed in `INTAKE_MAILBOXES` (env,
  default empty = feature off everywhere).
- **Direction-scoped**: Inbound only; internal senders never auto-create.
- **Noise-gated**: the same triage that keeps newsletters out of the queue
  keeps them out of the contact table; Tier-0 `admin_blast` senders are
  blocked before this point.
- **Schema-gated**: the intake path activates only when
  `contact.new_autocreatedby` exists in the target environment (the
  `has_attribute` probe, same pattern as the v2 request columns). PROD stays
  dormant until the 1.1.x solution import lands.
- **Reversible**: auto-created contacts are queryable by the marker
  (`new_autocreatedby ne null`); deleting one cascades its signals via the
  existing relationship behavior (RemoveLink — signals keep rows, lose the
  link) — cleanup never destroys correspondence history.

## Retroactive linking

When a human later merges/links an auto-created sender to a real investor
relationship (assigning an opportunity in the review queue, or CRM contact
merge), nothing special is needed: signals ride the contact record, thread
inheritance confirms future mail, and the aisummary accrues on the surviving
contact. No custom merge logic in v1 of this feature.

## Operator prerequisites (before PROD activation)

1. The pending manual solution import must include `contact.new_autocreatedby`
   (added to provision §12; re-exported in the same 1.1.x zip).
2. The **Engagement Ingestion role needs Create on Contact** in PROD (WS5
   added Write; Create is new). Without it the intake path fails per-row and
   the run log shows the errors.
3. Set `INTAKE_MAILBOXES=ir@exigentcap.com` in the PROD `.env`.

## Acceptance

- A CPA email to ir@ from an unknown address → contact exists (marked),
  signal Unmatched, ticket if actionable, visible in review queue + tracker.
- Same email re-synced → zero new rows (contact reused, signal upsert
  unchanged).
- The same sender writing to a non-intake mailbox → old behavior
  (`no_contact` drop).
- A newsletter to ir@ → no contact, no signal beyond existing noise handling.
