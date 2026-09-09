"""One-time repair for signals buried by the low-confidence gate.

Until 2026-09-09, a weak OPPORTUNITY match (confidence < review_min) wrote
inbound mail as matchstatus=Excluded + noisereason=low_confidence even when the
classifier had read it as a genuine REQUEST — so no Information Request was
created and the row was invisible to every dashboard KPI and the review queue
(the Phil Rosen case: "add my son to the Carta portal", 2026-09-08).

sync.py now rescues those at ingestion time. This pass heals the history the
same way, then leaves ticket creation to promote_requests.py — which already
applies the right guardrails (Inbound only, contact-associated, keyed on the
source signal so it is idempotent) but skips these rows precisely because the
bug marked them Excluded/not-meaningful.

Same guardrails here: INBOUND only, contact-associated only. The honest
matchconfidence is preserved — a weak opportunity match stays weak, it just
lands in the review queue instead of the bin.

Usage:  venv/bin/python -m ingestion.repair_rescued_signals [--apply]
        venv/bin/python -m ingestion.promote_requests [--apply]   # then this
"""
import argparse
from collections import Counter

from .config import Config
from .dataverse_client import DataverseClient
from .sync import say


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cfg = Config.from_env()
    ch, p = cfg.choices, cfg.prefix
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, p, apply=args.apply)

    say("pulling buried request signals…")
    rows = dv.query(
        f"{p}engagementsignals?$select={p}name,{p}timestamputc,{p}sender,"
        f"{p}snippet,{p}matchconfidence,_{p}contact_value,_{p}opportunity_value"
        f"&$filter={p}isinforequest eq true and {p}noisereason eq 'low_confidence'"
        f" and {p}direction eq {ch.direction['Inbound']}"
        f" and {p}matchstatus eq {ch.matchstatus['Excluded']}"
        f"&$orderby={p}timestamputc desc")

    # contact-associated only (belt and braces: the filter cannot express it)
    todo = [r for r in rows if r.get(f"_{p}contact_value")]
    skipped = len(rows) - len(todo)

    by_sender = Counter(r.get(f"{p}sender") or "(none)" for r in todo)
    say(f"{len(todo)} signal(s) to rescue"
        + (f"; {skipped} skipped (no contact)" if skipped else ""))
    for sender, n in by_sender.most_common(12):
        say(f"  {n:3d}  {sender}")

    say("")
    for r in todo[:15]:
        say(f"  {r[f'{p}timestamputc'][:16]}  conf={r.get(f'{p}matchconfidence')}"
            f"  {(r.get(f'{p}sender') or '')[:32]:32s} "
            f"{(r.get(f'{p}name') or '')[:46]}")
    if len(todo) > 15:
        say(f"  … and {len(todo) - 15} more")

    made = Counter()
    for r in todo:
        sid = r[f"{p}engagementsignalid"]
        has_opp = bool(r.get(f"_{p}opportunity_value"))
        body = {
            # top candidate recorded → Suggested; none → Unmatched. Both are
            # review-queue states, which is the whole point of the rescue.
            f"{p}matchstatus": ch.matchstatus["Suggested" if has_opp
                                              else "Unmatched"],
            f"{p}noisereason": None,
            f"{p}ismeaningful": True,
        }
        dv.patch(f"{p}engagementsignals", sid, body,
                 f"rescue signal {sid[:8]}… → "
                 f"{'Suggested' if has_opp else 'Unmatched'}")
        made["Suggested" if has_opp else "Unmatched"] += 1

    say("")
    mode = "APPLIED" if args.apply else "DRY RUN"
    say(f"[{mode}] {sum(made.values())} signal(s) rescued: {dict(made)}")
    if not args.apply:
        say("re-run with --apply, then: python -m ingestion.promote_requests "
            "--apply  (creates the Information Requests)")
    else:
        say("now run: venv/bin/python -m ingestion.promote_requests --apply")


if __name__ == "__main__":
    main()
