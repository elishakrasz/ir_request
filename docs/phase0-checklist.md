# Phase 0 — Operator Checklist (manual steps)

Everything below is done **by you, by hand**, in the Entra / M365 / Power Platform admin portals
and Exchange Online PowerShell. Nothing in this phase is automated. When every box is checked,
fill in the remaining values at the bottom and Phase 1 (DEV schema) can start.

> **App-reuse decision (2026-07-28):** the existing prospect-pipeline Entra app is reused —
> `CLIENT_ID f3c31b03-e0b8-40eb-8902-d69d6ff3a89a`, `TENANT_ID 6cd87acb-…240f` (already in `.env`).
> That app already holds PROD Dataverse rights, so the **Exchange RBAC scoping in section 2 is the
> load-bearing control** — it must be in place and verified before the first sync ever runs.

---

## 1. Extend the existing Entra app registration

- [ ] Entra admin center → App registrations → the prospect-pipeline app (`f3c31b03…`) →
      **API permissions → Add a permission → Microsoft Graph → Application permissions →
      `Mail.Read`**. Nothing else — no Dataverse `user_impersonation` (server-to-server Dataverse
      uses an application user + security role, section 3).
- [ ] Record the app's **service principal Object ID** (Entra → Enterprise applications → the app
      → Object ID). Needed for Exchange RBAC below — this is the *enterprise app* object id,
      **not** the app-registration object id.

## 2. Mailbox scoping (do this BEFORE the first sync — load-bearing control)

Preferred path: **Application RBAC** (resource-scoped role assignment). `ApplicationAccessPolicy`
is legacy per Microsoft — use it only as fallback (2b).

### 2a. Application RBAC (preferred)

```powershell
# Exchange Online PowerShell v3+ (Connect-ExchangeOnline), as Exchange admin

# 1. A management scope covering ONLY the in-scope mailboxes.
#    Simplest robust filter: membership of a mail-enabled security group you control.
#    First create (M365 admin center or PowerShell) a mail-enabled security group,
#    e.g. engagement-sync-scope@exigentcap.com, containing the ~10 mailboxes.

New-ManagementScope -Name "EngagementSyncMailboxes" `
  -RecipientRestrictionFilter "MemberOfGroup -eq 'CN=...'"   # use the group's DistinguishedName
  # (Get-DistributionGroup engagement-sync-scope@exigentcap.com).DistinguishedName

# 2. Register the app's service principal with Exchange (Object ID from section 1):
New-ServicePrincipal -AppId "f3c31b03-e0b8-40eb-8902-d69d6ff3a89a" `
  -ObjectId "<ENTERPRISE-APP-OBJECT-ID>" `
  -DisplayName "Prospect Pipeline / Engagement Dashboard"

# 3. Scoped role assignment — this is what confines Mail.Read to the group:
New-ManagementRoleAssignment -App "f3c31b03-e0b8-40eb-8902-d69d6ff3a89a" `
  -Role "Application Mail.Read" `
  -CustomResourceScope "EngagementSyncMailboxes"

# 4. Verify — must return Granted for an in-scope mailbox and NOT for an out-of-scope one:
Test-ServicePrincipalAuthorization -Identity "f3c31b03-e0b8-40eb-8902-d69d6ff3a89a" -Resource "<in-scope-user@exigentcap.com>"
Test-ServicePrincipalAuthorization -Identity "f3c31b03-e0b8-40eb-8902-d69d6ff3a89a" -Resource "<out-of-scope-user@exigentcap.com>"
```

- [ ] Mail-enabled security group created; the ~10 in-scope mailboxes are its only members
- [ ] `New-ManagementScope` / `New-ServicePrincipal` / `New-ManagementRoleAssignment` run
- [ ] `Test-ServicePrincipalAuthorization` verified for one in-scope AND one out-of-scope mailbox

### 2b. Fallback only — ApplicationAccessPolicy (legacy)

Only if App RBAC proves awkward in your tenant:

```powershell
New-ApplicationAccessPolicy -AppId "f3c31b03-e0b8-40eb-8902-d69d6ff3a89a" `
  -PolicyScopeGroupId engagement-sync-scope@exigentcap.com `
  -AccessRight RestrictAccess `
  -Description "Engagement dashboard: restrict Mail.Read to in-scope mailboxes"

Test-ApplicationAccessPolicy -AppId "f3c31b03-e0b8-40eb-8902-d69d6ff3a89a" -Identity <in-scope-user@exigentcap.com>
Test-ApplicationAccessPolicy -AppId "f3c31b03-e0b8-40eb-8902-d69d6ff3a89a" -Identity <out-of-scope-user@exigentcap.com>
```

- [ ] (fallback only) Policy created and tested both ways

## 3. Admin consent + Dataverse application user (DEV)

- [ ] App registration → **API permissions → Grant admin consent for Exigent** (Graph `Mail.Read`)
- [ ] Power Platform admin center → **DEV environment** → Settings → Users + permissions →
      **Application users → + New app user** → pick the app, choose the root business unit.
      (The app is presumably already an application user in PROD for the prospect pipeline —
      this step is for **DEV**, which is where all schema and ingestion work happens.)
- [ ] In the DEV environment (maker portal → Settings → Security roles), create a custom role
      **`Engagement Ingestion`** (copy from a minimal base, not from a broad role):
  - `Contact`, `Opportunity`, `Connection`, `Email` — **Read** (org scope)
  - New tables (created in Phase 1): `Engagement Signal`, `Information Request` —
    **Create / Read / Write / Append** (org scope)
  - `Contact`, `Opportunity`, `Systemuser` — **Append To** (org scope; required so the new tables'
    lookups to them can be set)
  - Nothing else. No delete, no share.
- [ ] Assign the role to the DEV application user
  - Note: the role can only reference the new tables **after Phase 1 creates them** — create the
    role now with the standard-table privileges, then revisit this box after Phase 1 import.
- [ ] **Also assign `System Customizer` to the DEV application user** — required so
      `solution/provision.py` can create the schema app-only (DEV only; PROD app user untouched).
      May be removed after Phase 1 completes; re-add for future schema changes.

## 4. Client secret

- [ ] Reusing the existing secret from the shared `.env` — already in place, nothing to create.
- [ ] Check its **expiry date** in Entra (Certificates & secrets) and set a calendar reminder —
      **rotation now affects BOTH the prospect pipeline and this project.**
- [ ] Key Vault migration remains a deployment-time task (Azure Functions phase).

## 5. Teams (Phase 5 — note only, no action now)

- [ ] Noted: `ChannelMessage.Read.All` + `getAllMessages` is a **protected API** — requires a
      Microsoft access request with business justification:
      <https://aka.ms/teamsgraph/requestaccess>
      Do not file the request yet; it is a Phase 5 decision. Licensing/metering status must be
      re-verified at that time.

---

## GATE — remaining values to close Phase 0 (fill into `.env`)

| Placeholder | Value | Status |
|---|---|---|
| `{TENANT_ID}` | `6cd87acb-0cb2-4d43-85aa-452ef20a240f` | ✅ resolved (shared app) |
| `{CLIENT_ID}` | `f3c31b03-e0b8-40eb-8902-d69d6ff3a89a` | ✅ resolved (shared app) |
| `{DEV_URL}` → `DATAVERSE_URL` | `https://exigentcrmdev.crm4.dynamics.com/` | ✅ supplied 2026-07-28 |
| `{PREFIX}` | `new_` | ✅ confirmed 2026-07-28 |
| `{ORG_DOMAINS}` | `exigentcap.com` | ✅ (all supplied mailboxes are on it) |
| `MAILBOXES` | 8 mailboxes (see `.env`) — ebrender, edavis, ir (shared), mgestetner, mravid, dmizrahi, yforman, mgannot | ✅ supplied 2026-07-28 |

Phase 1 (DEV schema build) starts only after every box above is checked and `.env` is complete.
