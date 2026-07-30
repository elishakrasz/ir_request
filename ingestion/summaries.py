"""WS5 — cached AI correspondence summaries per contact.

COST CONTROLS (stated + honored):
- Regenerate ONLY when confirmed signals newer than the stored watermark exist
  (two consecutive runs with no new mail → zero LLM calls).
- Input capped: last 30 days of confirmed, non-noise EMAIL signals (subject +
  snippet), truncated oldest-first to INPUT_CHAR_BUDGET.
- One model call per stale contact; per-run token usage logged.
- Scope: contacts of LIVE opportunities only (the dashboard cohort).

Model: claude-opus-5 (llm.SUMMARY_MODEL) — summaries are user-facing quality.
Teams signals are excluded by construction (v1 never ingested Teams).

Requires: Engagement Ingestion role needs WRITE on Contact (operator adds; the
job dry-runs without it).

Usage:
    python -m ingestion.summaries              # dry run (shows stale contacts)
    python -m ingestion.summaries --apply      # generate + store
    python -m ingestion.summaries --apply --contact <guid>   # on-demand refresh
"""
import argparse
import json
from datetime import datetime, timedelta, timezone

from . import llm
from .config import Config
from .dataverse_client import DataverseClient
from .sync import build_scope, clip, parse_ts, say

INPUT_CHAR_BUDGET = 24_000          # ≈6k tokens of correspondence per contact
SENTINEL = "No substantive correspondence in this period."

SYSTEM = (
    "You write correspondence summaries for a private-equity IR team. "
    "Given one investor contact's recent email exchanges (newest last), write "
    "3-4 sentences containing concrete specifics: the last substantive "
    "exchange and its date, commitments made in either direction, and anything "
    "awaiting a reply and by whom. Never use filler like 'they exchanged "
    "several emails' — name the actual matters discussed. If nothing "
    f"substantive happened, output exactly: {SENTINEL}"
)


def stale_contacts(dv, cfg, only_contact=None):
    """Contacts of live opps whose newest confirmed signal postdates their
    summary watermark. Returns [(contactid, name, watermark, newest, signals)]."""
    p, ch = cfg.prefix, cfg.choices
    since = (datetime.now(timezone.utc) - timedelta(days=30)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    opp_rows = dv.fetch_opportunities(cfg.fund_lookup)
    conn_rows = dv.fetch_connections()
    contact_ids = {o.get("_parentcontactid_value") for o in opp_rows}
    for cn in conn_rows:
        contact_ids.add(cn["_record1id_value"] if cn.get("record1objecttypecode") == 2
                        else cn["_record2id_value"])
    contact_rows = dv.fetch_contacts([i for i in contact_ids if i])
    opp_meta, email_map = build_scope(opp_rows, conn_rows, contact_rows)
    live_cids = {cid for _, (cid, oppids) in email_map.items()
                 if any(opp_meta.get(o, {}).get("live") for o in oppids)}

    rows = dv.query(
        f"{p}engagementsignals?$select={p}name,{p}snippet,{p}timestamputc,"
        f"{p}direction,_{p}contact_value"
        f"&$filter={p}timestamputc ge {since} and "
        f"{p}matchstatus eq {ch.matchstatus['Confirmed']} and "
        f"{p}ismeaningful eq true")
    by_contact = {}
    for r in rows:
        cid = r.get(f"_{p}contact_value")
        if cid in live_cids:
            by_contact.setdefault(cid, []).append(r)

    # fetch each contact's name + summary watermark in chunks
    meta = {}
    cids = [c for c in by_contact if not only_contact or c == only_contact]
    for i in range(0, len(cids), 20):
        flt = " or ".join(f"contactid eq {c}" for c in cids[i:i + 20])
        for c in dv.query("contacts?$select=contactid,fullname,"
                          f"new_aisummarywatermark&$filter={flt}"):
            meta[c["contactid"]] = c

    out = []
    for cid, sigs in by_contact.items():
        if only_contact and cid != only_contact:
            continue
        sigs.sort(key=lambda r: r[f"{p}timestamputc"])
        newest = sigs[-1][f"{p}timestamputc"]
        wm = (meta.get(cid) or {}).get("new_aisummarywatermark") or ""
        if only_contact or not wm or parse_ts(newest) > parse_ts(wm):
            out.append((cid, (meta.get(cid) or {}).get("fullname", "?"),
                        wm, newest, sigs))
    return out


def build_input(sigs, p, inbound_value):
    """Oldest-first truncation to the char budget (spec: truncate oldest-first)."""
    lines = []
    for r in sigs:
        who = "THEM" if r.get(f"{p}direction") == inbound_value else "US"
        lines.append(f"[{r[f'{p}timestamputc'][:10]}] {who}: "
                     f"{r.get(f'{p}name') or ''}\n"
                     f"{(r.get(f'{p}snippet') or '')[:400]}")
    while lines and sum(len(x) for x in lines) > INPUT_CHAR_BUDGET:
        lines.pop(0)   # oldest-first truncation
    return "\n\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--contact", help="force-refresh one contact id")
    args = ap.parse_args()
    cfg = Config.from_env()
    p = cfg.prefix
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, p, apply=args.apply)

    stale = stale_contacts(dv, cfg, only_contact=args.contact)
    say(f"{len(stale)} contact(s) need a summary refresh")
    if not stale:
        say("[llm] zero LLM calls this run (all watermarks current)")
        return
    if not args.apply:
        for cid, name, wm, newest, sigs in stale:
            print(f"  {name[:34]:34} wm={wm[:19] or '—':19} newest={newest[:19]} "
                  f"({len(sigs)} signals)")
        print("\nDRY RUN — --apply to generate.")
        return
    if not llm.available():
        say("no ANTHROPIC_API_KEY — cannot generate")
        return

    import time

    import anthropic
    client = anthropic.Anthropic(api_key=llm._api_key(), max_retries=4)
    tin = tout = 0
    for cid, name, wm, newest, sigs in stale:
        resp = None
        for attempt, pause in enumerate((0, 15, 45)):
            if pause:
                time.sleep(pause)
            try:
                resp = client.messages.create(
                    model=llm.SUMMARY_MODEL,
                    max_tokens=1024,
                    system=[{"type": "text", "text": SYSTEM,
                             "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user",
                               "content": f"Contact: {name}\n\n"
                               + build_input(sigs, p,
                                             cfg.choices.direction["Inbound"])}],
                )
                break
            except (anthropic.RateLimitError, anthropic.InternalServerError,
                    anthropic.APIStatusError) as e:
                say(f"  {name}: {type(e).__name__} (attempt {attempt + 1}) — "
                    "backing off")
        if resp is None:
            # watermark not advanced → this contact retries on the next run
            say(f"  {name}: skipped after retries (will retry next run)")
            continue
        if resp.stop_reason == "refusal":
            say(f"  {name}: refused — skipped")
            continue
        text = next((b.text for b in resp.content if b.type == "text"), "").strip()
        tin += resp.usage.input_tokens
        tout += resp.usage.output_tokens
        dv.patch("contacts", cid, {
            "new_aisummary": clip(text, 4000),
            "new_aisummaryat": datetime.now(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "new_aisummarywatermark": newest,
        }, f"summary {name[:30]}")
        say(f"  {name[:40]}: {len(text)} chars")
    say(f"[llm] {len(stale)} summaries, {tin} in / {tout} out tokens "
        f"({llm.SUMMARY_MODEL})")


if __name__ == "__main__":
    main()
