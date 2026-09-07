# scripts/ — one-off analysis & verification

Read-only helpers that are not part of the ingestion service. Run from the
repo root with the project venv; outputs land in gitignored `reports/` (PII).

| Script | Purpose |
|---|---|
| `ir_triage/ir_taxonomy.py` | Pull 2 years of ir@ inbound metadata (+bodyPreview) → `reports/ir_inbox_raw.jsonl` |
| `ir_triage/ir_split.py` | Split raw pull into admin/platform blasts vs human threads |
| `ir_triage/ir_aggregate.py` | Volumes, folders, sender domains, noise split |
| `ir_triage/ir_count.py` | Rough keyword bucket sizing over human threads |
| `ir_triage/ir_classify.py` | Prototype LLM triage over human threads (fed `docs/ir-triage-categories.md`) |
| `verify_v2.py` | Classifier-v2 verification over recent PROD inbound (read-only) |
| `prodcheck.py` | Did the EngagementDashboard solution land in PROD? |
| `seed_rfi.py` | WS6 acceptance: seeded emails → urgency tags (live LLM) |

Pipeline for the taxonomy work: `ir_taxonomy.py` → `ir_split.py` →
`ir_aggregate.py` / `ir_count.py` / `ir_classify.py`.
