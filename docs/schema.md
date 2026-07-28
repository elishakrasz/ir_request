# Schema — EngagementDashboard solution

Phase 1 deliverable. The **source of truth for column definitions is
[`solution/provision.py`](../solution/provision.py)** — this document explains the shape,
the decisions, and the two manual steps (rollups, export).

## How the schema gets into an environment

```
DEV:   python solution/provision.py            # dry run — review output
       python solution/provision.py --apply    # idempotent creates via Web API
       (maker portal: create 6 rollup columns — section below)
       solution/export.sh                      # capture declarative source → solution/src/

PROD:  manual maker-portal import of solution/build/EngagementDashboard_managed.zip
       — never CLI (house rule 1; provision.py hard-refuses PROD URLs)
```

**Deviation from the spec's "hand-authored declarative XML" preference, for gate review:**
schema is provisioned by an idempotent, dry-run-first Web API script instead of
hand-written `customizations.xml`. Rationale: (a) rollup definitions are opaque workflow
XAML that cannot be sanely hand-authored in XML anyway; (b) the Web API validates each
component individually with clear errors, vs. one monolithic import failure; (c) dry-run +
idempotency match house rules 2/4, which a raw XML import cannot honor. The declarative
source the spec wants still exists — `solution/src/`, produced by `export.sh` after
provisioning, is the committed re-creatable artifact.

Provisioning auth is app-only (shared Entra app). The **DEV application user needs
System Customizer** for schema creation — checklist section 3.

## Tables

### `new_engagementsignal` — Engagement Signal (organization-owned)

One row per message × matched contact. Org-owned (no `ownerid`): rows are machine-written
facts, not work items — ownership semantics would be noise.

Columns: see `provision.py` §4. Highlights:

| Column | Note |
|---|---|
| `new_name` (primary) | Subject, truncated to 200 |
| `new_timestamputc` | DateTime, **UserLocal behavior = stored as UTC**, rendered in viewer TZ |
| `new_messagekeyhash` | SHA-256 hex of `new_messagekey`; the alternate-key column (raw internetMessageIds can exceed 255 chars; a 512-char text column would blow the 900-byte key-index cap) |
| `new_rfistatus` | **Overdue value is reserved — nothing writes it**; overdue-ness is derived from the request's `duedate` in the app layer |
| `new_sourcelink` | Graph `webLink` (Url format) — opens only for the synced mailbox's owner |
| `new_ismeaningful` | default **Yes**; ingestion sets No for auto-replies/bounces/mass mail; every KPI filters on Yes |
| `new_modifiedbyhint` | human identity behind dashboard writes (service principal performs them) |

**Alternate key** `new_signalidentity` on (`new_messagekeyhash`, `new_contact`) — the
upsert idempotency key. Index activation is **async** after creation; confirm the key
shows Active in the maker portal before the first ingestion run.

**`new_conversationid` indexing:** plain secondary indexes aren't creatable via the public
API/solution XML. Thread-inheritance queries filter on this column; if DEV-scale testing
shows it slow, raise a Microsoft support request for an index or accept the scan (~10
mailboxes ≈ small table). Documented as a known limitation.

### `new_inforequest` — Information Request (user-owned)

The working record for RFIs, with its own lifecycle. **User-owned deliberately: the native
`ownerid` field IS the spec's "owner (user lookup)"** — no custom owner column.

Columns: see `provision.py` §5 (category / receiveddate / duedate / completeddate /
status / firstresponseminutes / aigenerated / humanconfirmed / modifiedbyhint, plus
lookups to opportunity, contact, and source signal).

There is **no Overdue status** — overdue = `status ∉ {Completed, Cancelled} AND
now > duedate`, computed in the app layer, so overdue items can never drift out of the
open count.

### Opportunity extension columns

`new_oppcode` (20, explicit-match token), `new_aliases` (memo, matching evidence),
`new_activemonitoring`, `new_monitoringstartdate` (DateOnly), `new_teamschannelid`
(reserved, Phase 5).

### Plain table vs. custom activity

Operator decided **plain table** (2026-07-28): the Streamlit app is the display surface,
tracked emails already appear on the D365 timeline, and the activity choice is
effectively irreversible.

## Rollup columns — manual maker-portal step (~10 min, DEV)

Rollups can't be authored via API/XML (workflow XAML). Create these **six** in the maker
portal, inside the **EngagementDashboard** solution (open the solution first, then the
table → New column), so the export captures them:

On **Contact** (3):

| Display name | Schema | Type | Rollup definition |
|---|---|---|---|
| Last Inbound | `new_lastinbounddate` | DateTime, rollup | Related: Engagement Signals (Contact); MAX of Timestamp (UTC); filter Direction = Inbound |
| Last Outbound | `new_lastoutbounddate` | DateTime, rollup | same, filter Direction = Outbound |
| Open RFI Count | `new_openrficount` | Whole number, rollup | Related: **Information Requests** (Contact); COUNT; filter Status ≠ Completed AND Status ≠ Cancelled |

On **Opportunity** (3): identical trio, related via the Opportunity lookups
(`new_lastinbounddate`, `new_lastoutbounddate`, `new_openrficount`).

Notes:
- Rollups recalc on a schedule (~hourly at best) — the dashboard treats them as
  convenience fields, never real-time truth.
- Rolling windows (7/30-day counts) are **not possible** as rollups — computed in pandas
  in the app layer.
- After creating all six: run `solution/export.sh` and commit the `solution/src/` diff.

## Choice values

All choices are local option sets. Values are `optionvalueprefix × 10000 + index` in
declaration order (see `provision.py`); the prefix comes from the reused `new` publisher
(or 65100 if the script had to create one). **Never reorder or remove options once data
exists** — append only.

## Security role (reference)

`Engagement Ingestion` role (checklist §3): read on contact/opportunity/connection/email;
create/read/write/append on the two new tables; append-to on contact/opportunity/systemuser.
Provisioning additionally needs System Customizer on the DEV app user — that can be
removed after Phase 1 if desired (re-add for schema changes).
