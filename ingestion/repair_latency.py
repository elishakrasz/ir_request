"""WS3 migration — recompute ALL stored response latencies under the v2
semantics (inbound-anchored, business minutes, Sun–Thu Asia/Jerusalem).

v1 stored wall-clock latency on OUTBOUND rows; v2 stores business-minute
latency on INBOUND rows. This tool nulls the former and writes the latter.
Dry run default; --apply commits.
"""
import argparse
from collections import defaultdict

from .config import Config
from .dataverse_client import DataverseClient
from .latency import compute_response_pairs
from .sync import parse_ts, say


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    cfg = Config.from_env()
    p, ch = cfg.prefix, cfg.choices
    rev_dir = {v: k for k, v in ch.direction.items()}
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, p, apply=args.apply)

    say("fetching signals")
    rows = dv.query(
        f"{p}engagementsignals?$select={p}engagementsignalid,{p}conversationid,"
        f"{p}direction,{p}timestamputc,{p}responselatencymin,{p}ismeaningful,"
        f"{p}matchstatus,_{p}contact_value")
    excluded = ch.matchstatus["Excluded"]
    by_conv = defaultdict(list)
    for r in rows:
        if not r.get(f"{p}timestamputc"):
            continue
        by_conv[r.get(f"{p}conversationid") or r[f"{p}engagementsignalid"]].append(r)

    set_patches, null_patches = [], []
    for conv, sigs in by_conv.items():
        eligible = [s for s in sigs if s.get(f"{p}ismeaningful")
                    and s.get(f"{p}matchstatus") != excluded]
        model = [{"id": s[f"{p}engagementsignalid"],
                  "contact": s.get(f"_{p}contact_value"),
                  "direction": rev_dir.get(s.get(f"{p}direction"), "Internal"),
                  "ts": parse_ts(s[f"{p}timestamputc"]),
                  "latency": None,          # force full recompute
                  "meaningful": True} for s in eligible]
        pairs = compute_response_pairs(model)
        for s in sigs:
            sid = s[f"{p}engagementsignalid"]
            stored = s.get(f"{p}responselatencymin")
            want = pairs.get(sid)   # only inbounds can have a value
            if want is not None and stored != want:
                set_patches.append((sid, want))
            elif want is None and stored is not None:
                null_patches.append(sid)

    print(f"\nconversations: {len(by_conv)}")
    print(f"inbound latencies to set: {len(set_patches)}")
    print(f"stale values to null (old outbound-anchored + unpaired): {len(null_patches)}")
    if not args.apply:
        print("\nDRY RUN — re-run with --apply to commit.")
        return
    n = 0
    for sid, v in set_patches:
        dv.patch(f"{p}engagementsignals", sid, {f"{p}responselatencymin": v},
                 f"lat {sid[:8]}")
        n += 1
        if n % 500 == 0:
            say(f"  {n}")
    for sid in null_patches:
        dv.patch(f"{p}engagementsignals", sid, {f"{p}responselatencymin": None},
                 f"latnull {sid[:8]}")
        n += 1
        if n % 500 == 0:
            say(f"  {n}")
    say(f"done — {n} patches")


if __name__ == "__main__":
    main()
