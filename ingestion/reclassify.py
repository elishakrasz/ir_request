"""WS1 backfill — reprocess existing signals through the v2 pipeline
(noise gate + boosters + thresholds).

Dry run is the DEFAULT (spec 1.4 + house rule 4): prints counts by
old→new transition and up to 20 sample rows per resulting bucket; --apply
performs the patches.

Scope rules:
- Human assignments (matchmethod=Manual) are NEVER touched.
- The noise gate runs over ALL signals (a previously-Confirmed newsletter
  still becomes noise).
- Re-matching runs only over Suggested/Unmatched rows (v1 auto-Confirmed
  rows are left standing to avoid mass churn — recorded in REVISION_NOTES).
- Stored signals carry no headers, so header-based rules can't apply
  retroactively; blocklist + LLM cover the backfill.

Usage:
    python -m ingestion.reclassify             # dry run + report
    python -m ingestion.reclassify --apply
"""
import argparse
import json
from collections import Counter, defaultdict

from . import llm, matching, noise
from .config import Config
from .dataverse_client import DataverseClient
from .sync import build_scope, say


def fetch_all_signals(dv, p):
    return dv.query(
        f"{p}engagementsignals?$select={p}engagementsignalid,{p}name,{p}sender,"
        f"{p}snippet,{p}conversationid,{p}messagekey,{p}matchstatus,{p}matchmethod,"
        f"{p}matchconfidence,{p}noisereason,{p}ismeaningful,"
        f"_{p}contact_value,_{p}opportunity_value")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    cfg = Config.from_env()
    p, ch = cfg.prefix, cfg.choices
    rev_status = {v: k for k, v in ch.matchstatus.items()}
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, p, apply=args.apply)

    say("fetching scope + signals + regarding map")
    opp_rows = dv.fetch_opportunities(cfg.fund_lookup)
    conn_rows = dv.fetch_connections()
    contact_ids = {o.get("_parentcontactid_value") for o in opp_rows}
    for cn in conn_rows:
        contact_ids.add(cn["_record1id_value"] if cn.get("record1objecttypecode") == 2
                        else cn["_record2id_value"])
    contact_rows = dv.fetch_contacts([i for i in contact_ids if i])
    opp_meta, email_map = build_scope(opp_rows, conn_rows, contact_rows)
    contact_opps = defaultdict(set)
    for e, (cid, oppids) in email_map.items():
        contact_opps[cid] |= set(oppids)
    regarding = dv.fetch_regarding_map(cfg.ingest_floor.strftime("%Y-%m-%dT%H:%M:%SZ"))
    signals = fetch_all_signals(dv, p)
    say(f"{len(signals)} signals; {len(opp_meta)} opps "
        f"({sum(1 for m in opp_meta.values() if m['live'])} live); "
        f"{len(regarding)} regarding entries")

    # conv → confirmed opp (for thread inheritance during re-match)
    conv_opp = {}
    for s in signals:
        if s.get(f"{p}matchstatus") == ch.matchstatus["Confirmed"] and \
                s.get(f"_{p}opportunity_value") and s.get(f"{p}conversationid"):
            conv_opp.setdefault(s[f"{p}conversationid"], s[f"_{p}opportunity_value"])

    manual = ch.matchmethod["Manual"]
    plan, samples = [], defaultdict(list)      # (sig, patch, bucket, old_bucket)
    transitions = Counter()
    candidates, cand_rows = [], {}

    def bucket_of(s):
        st = rev_status.get(s.get(f"{p}matchstatus"), "?")
        if st == "Excluded":
            return "noise"
        if st == "Confirmed":
            return "auto_confirmed"
        return "needs_review"

    for s in signals:
        if s.get(f"{p}matchmethod") == manual:
            transitions[("manual", "manual")] += 1
            continue
        old = bucket_of(s)
        sender = (s.get(f"{p}sender") or "").lower()
        subject = s.get(f"{p}name") or ""
        snippet = s.get(f"{p}snippet") or ""
        verdict, reason = noise.gate(
            sender, subject, snippet, cfg.rules,
            sender_is_matched_contact=sender in email_map,
            sender_is_internal=matching.domain_of(sender) in cfg.org_domains)
        if verdict == "candidate" and old != "noise":
            sid = s[f"{p}engagementsignalid"]
            candidates.append({"key": sid, "sender": sender,
                               "subject": subject, "snippet": snippet[:200]})
            cand_rows[sid] = s
            continue
        _classify_row(s, old, verdict, reason, plan, transitions, samples,
                      cfg, p, ch, opp_meta, contact_opps, conv_opp, regarding,
                      rev_status)

    # LLM fallback for the undecidable candidates (batched + cached)
    verdicts = llm.classify_noise(candidates, log=say) if candidates else {}
    for sid, s in cand_rows.items():
        old = bucket_of(s)
        reason = noise.llm_label_to_noise_reason(verdicts.get(sid, ""))
        v = "noise" if reason else "ok"
        _classify_row(s, old, v, reason, plan, transitions, samples,
                      cfg, p, ch, opp_meta, contact_opps, conv_opp, regarding,
                      rev_status)

    # ── report ───────────────────────────────────────────────────────────────
    print("\n=== transitions (old → new) ===")
    for (old, new), n in sorted(transitions.items(), key=lambda x: -x[1]):
        print(f"  {old:>14} → {new:<14} {n}")
    lowconf = samples.pop("_lowconf_by_fund", [])
    if lowconf:
        print(f"\n=== low_confidence→noise, by top-candidate fund "
              f"(non-live opps — {len(lowconf)} rows) ===")
        for fund, n in Counter(lowconf).most_common(15):
            print(f"  {n:5d}  {fund}")

    print(f"\npatches planned: {len(plan)}")
    for bucket in ("noise", "auto_confirmed", "needs_review"):
        rows = samples.get(bucket, [])
        if rows:
            print(f"\n--- sample rows → {bucket} (up to 20) ---")
            for r in rows[:20]:
                print(f"  {r}")

    if not args.apply:
        print("\nDRY RUN — re-run with --apply to commit.")
        return
    say(f"applying {len(plan)} patches")
    done = 0
    for sid, patch in plan:
        dv.patch(f"{p}engagementsignals", sid, patch, f"reclassify {sid[:8]}")
        done += 1
        if done % 500 == 0:
            say(f"  {done}/{len(plan)}")
    say(f"done — {done} rows updated")


def _classify_row(s, old, verdict, reason, plan, transitions, samples,
                  cfg, p, ch, opp_meta, contact_opps, conv_opp, regarding,
                  rev_status):
    sid = s[f"{p}engagementsignalid"]
    label = (f"{(s.get(f'{p}sender') or '')[:36]:36} | "
             f"{(s.get(f'{p}name') or '')[:56]}")
    if verdict == "noise":
        new = "noise"
        patch = {f"{p}matchstatus": ch.matchstatus["Excluded"],
                 f"{p}noisereason": reason[:100],
                 f"{p}ismeaningful": False}
    elif old == "auto_confirmed":
        # leave v1 auto-confirmed matches standing (see module docstring)
        transitions[(old, old)] += 1
        return
    else:
        cid = s.get(f"_{p}contact_value")
        oppid, method, conf = matching.resolve_opportunity_v2(
            s.get(f"{p}name") or "", s.get(f"{p}snippet") or "",
            contact_opps.get(cid, set()), opp_meta,
            conv_opp.get(s.get(f"{p}conversationid")),
            regarding_opp=regarding.get(s.get(f"{p}messagekey")))
        new = matching.disposition_for(conf, cfg)
        if new == "auto_confirmed":
            patch = {f"{p}matchstatus": ch.matchstatus["Confirmed"],
                     f"{p}matchmethod": ch.matchmethod[method],
                     f"{p}matchconfidence": conf,
                     f"{p}opportunity@odata.bind": f"/opportunities({oppid})"}
        elif new == "noise":
            patch = {f"{p}matchstatus": ch.matchstatus["Excluded"],
                     f"{p}noisereason": "low_confidence",
                     f"{p}matchconfidence": conf,
                     f"{p}ismeaningful": False}
        else:
            patch = {f"{p}matchconfidence": conf}
            if oppid:
                patch[f"{p}opportunity@odata.bind"] = f"/opportunities({oppid})"
            if old == "needs_review":   # unchanged bucket, minor confidence touch-up
                transitions[(old, new)] += 1
                if conf != s.get(f"{p}matchconfidence") or \
                        (oppid and oppid != s.get(f"_{p}opportunity_value")):
                    plan.append((sid, patch))
                    samples[new].append(label)
                return
        if new == "noise" and verdict != "noise":   # low_confidence path
            cand_fund = opp_meta.get(oppid or "", {}).get("fund_name") or "(none)"
            samples.setdefault("_lowconf_by_fund", []).append(cand_fund)
    transitions[(old, new)] += 1
    plan.append((sid, patch))
    samples[new].append(label)


if __name__ == "__main__":
    main()
