"""Split ir@ inbound into admin/platform blasts vs human correspondence.

Outputs (reports/, gitignored):
  ir_admin_templates.txt — bulk sender subject templates with counts
  ir_human.tsv           — deduped human threads (the triage-relevant set)
"""
import json
import re
from collections import Counter
from pathlib import Path

RAW = Path(__file__).resolve().parents[2] / "reports" / "ir_inbox_raw.jsonl"
ADMIN = Path(__file__).resolve().parents[2] / "reports" / "ir_admin_templates.txt"
HUMAN = Path(__file__).resolve().parents[2] / "reports" / "ir_human.tsv"

BULK_SENDERS = re.compile(
    r"@(apexgroup\.com|mail\.investors\.tzurmanagement\.com|"
    r"backstopsolutions\.com|carta\.com)$", re.I)
NOISE_SUBJECT = re.compile(
    r"^(automatic reply|auto(-|matic )?reply|out of office|undeliverable|"
    r"delivery (status|has failed)|mail delivery|accepted:|declined:|"
    r"tentative:|canceled:|read:)", re.I)
RE_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|tr)\s*:\s*)+", re.I)
WS = re.compile(r"\s+")


def template(subject: str) -> str:
    s = RE_PREFIX.sub("", subject or "").strip()
    s = re.sub(r"\d+", "#", s)
    return WS.sub(" ", s)


def main():
    msgs = [json.loads(l) for l in RAW.open(encoding="utf-8")]
    msgs.sort(key=lambda m: m["received"] or "")

    admin = Counter()
    human_all = []
    for m in msgs:
        if m["folder"] in ("Junk Email", "Deleted Items/Junk Email"):
            continue
        if NOISE_SUBJECT.search(m["subject"] or ""):
            continue
        if BULK_SENDERS.search(m["from_addr"] or ""):
            admin[(m["from_addr"], template(m["subject"]))] += 1
        else:
            human_all.append(m)

    with ADMIN.open("w", encoding="utf-8") as f:
        f.write(f"bulk/admin messages: {sum(admin.values())} "
                f"across {len(admin)} templates\n\n")
        for (sender, tpl), n in admin.most_common():
            f.write(f"{n:5d}  {sender}  ::  {tpl}\n")

    # dedupe human mail: one row per (sender, normalized subject)
    seen, rows = set(), []
    for m in human_all:
        key = (m["from_addr"], template(m["subject"]).lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append(m)

    with HUMAN.open("w", encoding="utf-8") as f:
        f.write("received\tfrom\tsubject\tpreview\n")
        for m in rows:
            f.write("\t".join([
                (m["received"] or "")[:10],
                m["from_addr"],
                WS.sub(" ", m["subject"] or "")[:110],
                WS.sub(" ", m["preview"] or "")[:180],
            ]) + "\n")
    print(f"admin msgs: {sum(admin.values())} ({len(admin)} templates) -> {ADMIN}")
    print(f"human msgs: {len(human_all)}, deduped rows: {len(rows)} -> {HUMAN}")


if __name__ == "__main__":
    main()
