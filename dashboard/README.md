# Investor Engagement Dashboard

```bash
# from the repo root (venv has streamlit/plotly/pandas installed)
venv/bin/streamlit run dashboard/app.py
# opens http://localhost:8501
```

Reads live from the DEV Dataverse via the shared app registration (`.env`),
cached ~5 minutes (sidebar ↻ forces a refresh).

## Views

- **Deal view** — per-opportunity: KPI row, alerts, contact health (RAG),
  timeline, contact drill-down with response-latency sparkline.
- **Fund cohort** — fund + date range → all correspondence from contacts
  connected to that fund's opportunities, **independent of per-email match
  status** (cohort-based, complete even while items sit in the review queue).
- **Review queue** — Suggested/Unmatched signals; one-click Confirm (assign
  opportunity) or Exclude. Requires your name in the sidebar — every action is
  stamped into `modifiedbyhint` (the service principal performs the write, so
  this is the human audit trail). Confirmations teach the matcher via thread
  inheritance.
- **Firm overview** — cross-deal risk table, worst first.

## Notes

- All engagement KPIs count `ismeaningful=true`, non-Excluded signals only;
  opportunity/fund-level message counts dedupe on `messagekeyhash`.
- `sourcelink` opens the message **in the mailbox it was synced from** — only
  that mailbox's owner can open it. Expected, not a bug.
- Rollup columns in Dataverse refresh ~hourly; this app computes time-window
  stats in pandas from the signal rows, so it does not depend on them.
- Auth hardening (Azure App Service + Easy Auth) is a deployment-time task;
  locally the name box is the identity stand-in.
