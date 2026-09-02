"""Aggregate reports/ir_inbox_raw.jsonl for taxonomy work (read-only, local).

Outputs (all in reports/, gitignored):
  ir_inbox_stats.txt    — volumes, folders, top sender domains, noise split
  ir_inbox_threads.tsv  — one row per conversation thread-starter (real mail only)
"""
import json
import re
from collections import Counter
from pathlib import Path

RAW = Path(__file__).parent / "reports" / "ir_inbox_raw.jsonl"
STATS = Path(__file__).parent / "reports" / "ir_inbox_stats.txt"
THREADS = Path(__file__).parent / "reports" / "ir_inbox_threads.tsv"

NOISE_SUBJECT = re.compile(
    r"^(automatic reply|auto(-|matic )?reply|out of office|undeliverable|"
    r"delivery (status|has failed)|mail delivery|accepted:|declined:|"
    r"tentative:|canceled:|read:)", re.I)
NOISE_SENDER = re.compile(
    r"(^|\.)(no-?reply|noreply|donotreply|mailer-daemon|postmaster|"
    r"notifications?|newsletter|marketing|info|alerts?|updates?)@", re.I)
NOISE_PREVIEW = re.compile(r"unsubscribe|view (this email )?in (your )?browser", re.I)


def noise_reason(m):
    if NOISE_SUBJECT.search(m["subject"] or ""):
        return "subject"
    if NOISE_SENDER.search(m["from_addr"] or ""):
        return "sender"
    if NOISE_PREVIEW.search(m["preview"] or ""):
        return "bulk-preview"
    return None


def main():
    msgs = [json.loads(l) for l in RAW.open(encoding="utf-8")]
    msgs.sort(key=lambda m: m["received"] or "")

    domains = Counter(m["from_addr"].split("@")[-1] for m in msgs if m["from_addr"])
    folders = Counter(m["folder"] for m in msgs)
    noise = Counter()
    real = []
    for m in msgs:
        r = noise_reason(m)
        if r or m["folder"] in ("Junk Email", "Deleted Items/Junk Email"):
            noise[r or "junk-folder"] += 1
        else:
            real.append(m)

    # thread starters: first kept inbound message per conversationId
    seen, starters = set(), []
    for m in real:
        cid = m["conversationId"] or m["internetMessageId"]
        if cid in seen:
            continue
        seen.add(cid)
        starters.append(m)

    by_month = Counter((m["received"] or "")[:7] for m in real)
    with STATS.open("w", encoding="utf-8") as f:
        f.write(f"total inbound: {len(msgs)}\n")
        f.write(f"noise: {sum(noise.values())} {dict(noise)}\n")
        f.write(f"real messages: {len(real)}  threads: {len(starters)}\n\n")
        f.write("folders:\n")
        for k, v in folders.most_common():
            f.write(f"  {v:6d}  {k}\n")
        f.write("\ntop 60 sender domains (all inbound):\n")
        for k, v in domains.most_common(60):
            f.write(f"  {v:6d}  {k}\n")
        f.write("\nreal messages by month:\n")
        for k in sorted(by_month):
            f.write(f"  {k}  {by_month[k]}\n")

    clean = re.compile(r"\s+")
    with THREADS.open("w", encoding="utf-8") as f:
        f.write("received\tfolder\tfrom\tsubject\tpreview\n")
        for m in starters:
            f.write("\t".join([
                (m["received"] or "")[:10],
                m["folder"],
                m["from_addr"],
                clean.sub(" ", m["subject"] or "")[:120],
                clean.sub(" ", m["preview"] or "")[:160],
            ]) + "\n")
    print(f"stats -> {STATS}\nthreads -> {THREADS} ({len(starters)} rows)")


if __name__ == "__main__":
    main()
