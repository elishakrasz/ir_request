"""Orchestrates one ingestion run (spec Phase 2).

Dry-run is the DEFAULT: logs intended upserts (counts + sample rows), writes
nothing, and does not advance delta tokens. --apply performs writes.

Usage:
    python -m ingestion.sync                # dry run
    python -m ingestion.sync --apply
    python -m ingestion.sync --mailbox ir@exigentcap.com   # limit scope
"""
import argparse
import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .classify import classify
from .config import Config, STATE_DIR
from .latency import compute_latencies
from . import matching

FOLDERS = ("inbox", "sentitems")

# Stable message facts that may be corrected on re-sync. Volatile/owned-elsewhere
# fields (provenance, rfistatus, isinforequest, responselatencymin) are set at
# create time only — comparing them would cause perpetual re-patches and stomp
# later-pass / human updates (no-silent-mutation, house rule 2).
STABLE_FIELDS = ("name", "sender", "timestamputc", "conversationid", "sourcelink",
                 "participants", "direction", "snippet", "messagekey", "ismeaningful")


def parse_ts(s: str) -> datetime:
    """Always tz-aware: DateOnly columns (e.g. monitoringstartdate) come back
    as bare dates — treat them as UTC midnight, or comparisons explode."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def add_business_days(d: datetime, n: int) -> datetime:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def code_version() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10,
                              cwd=Path(__file__).parent).stdout.strip() or "dev"
    except Exception:
        return "dev"


def build_scope(opp_rows, conn_rows, contact_rows, roles_by_id=None, allowed_roles=None):
    """Pure: raw Dataverse rows → (opp_meta, email_map).
    opp_meta: {oppid: {name, oppcode, aliases[], active, startdate}}
    email_map: {email: (contactid, set(oppids))}"""
    opp_meta, contact_opps = {}, {}

    def link(cid, oid):
        if cid and oid in opp_meta:
            contact_opps.setdefault(cid, set()).add(oid)

    for o in opp_rows:
        aliases = [a.strip() for a in (o.get("new_aliases") or "").replace(",", "\n").split("\n")
                   if a.strip()]
        start = o.get("new_monitoringstartdate")
        fund_key = next((k for k in o if k.startswith("_") and k.endswith("_value")
                         and "fundorspv" in k), None)
        opp_meta[o["opportunityid"]] = {
            "name": o.get("name") or "",
            "oppcode": o.get("new_oppcode") or "",
            "aliases": aliases,
            "active": bool(o.get("new_activemonitoring")),
            "startdate": parse_ts(start) if start else None,
            "fund": o.get(fund_key) if fund_key else None,
            "fund_name": o.get(f"{fund_key}@OData.Community.Display.V1.FormattedValue", "")
            if fund_key else "",
        }
    for o in opp_rows:
        link(o.get("_parentcontactid_value"), o["opportunityid"])
        # customerid counts only when it is a contact (lookuplogicalname annotation)
        if o.get("_customerid_value@Microsoft.Dynamics.CRM.lookuplogicalname") == "contact":
            link(o.get("_customerid_value"), o["opportunityid"])
    for c in conn_rows:
        if c.get("record1objecttypecode") == 2:
            cid, oid = c.get("_record1id_value"), c.get("_record2id_value")
        else:
            cid, oid = c.get("_record2id_value"), c.get("_record1id_value")
        if allowed_roles and roles_by_id is not None:
            names = {roles_by_id.get(c.get("_record1roleid_value"), ""),
                     roles_by_id.get(c.get("_record2roleid_value"), "")}
            if not names & set(allowed_roles):
                continue
        link(cid, oid)

    email_map = {}
    for c in contact_rows:
        cid = c["contactid"]
        for f in ("emailaddress1", "emailaddress2", "emailaddress3"):
            e = (c.get(f) or "").strip().lower()
            if e:
                email_map.setdefault(e, (cid, contact_opps.get(cid, set())))
    return opp_meta, email_map


class SyncRun:
    def __init__(self, cfg: Config, graph, dv, apply: bool,
                 state_dir: Path = STATE_DIR, codeversion: str = "dev"):
        self.cfg, self.graph, self.dv, self.apply = cfg, graph, dv, apply
        self.state_dir = Path(state_dir)
        self.runid = (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                      + "-" + uuid.uuid4().hex[:6])
        self.codeversion = codeversion
        self.counts = {"mailboxes": {}, "creates": 0, "patches": 0, "unchanged": 0,
                       "excluded": 0, "no_contact": 0, "below_floor": 0,
                       "rfi_created": 0, "latency_patched": 0, "answered": 0}
        self.samples = []
        self.touched_convs = set()
        self.new_signal_rfis = []   # (signal_id, msg, category)

    # ── delta state ──────────────────────────────────────────────────────────
    def _state_file(self, mailbox, folder) -> Path:
        return self.state_dir / "delta" / f"{mailbox}__{folder}.json"

    def _load_delta(self, mailbox, folder):
        f = self._state_file(mailbox, folder)
        return json.loads(f.read_text())["deltaLink"] if f.exists() else None

    def _save_delta(self, mailbox, folder, link):
        if not (self.apply and link):
            return  # dry-run never advances tokens
        f = self._state_file(mailbox, folder)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"deltaLink": link, "saved": self.runid}))

    # ── message prep ─────────────────────────────────────────────────────────
    def _prep(self, m) -> bool:
        """Annotate _ts/_sender/_participants/_hash. False → skip (below floor)."""
        ts = m.get("sentDateTime") or m.get("receivedDateTime")
        m["_ts"] = parse_ts(ts)
        if m["_ts"] < self.cfg.ingest_floor:
            return False
        m["_sender"] = matching.addr_of(m.get("from"))
        recips = [matching.addr_of(r) for r in
                  (m.get("toRecipients") or []) + (m.get("ccRecipients") or [])
                  + (m.get("bccRecipients") or [])]
        m["_recipients"] = [r for r in recips if r]
        m["_participants"] = sorted({a for a in [m["_sender"], *m["_recipients"]] if a})
        key = m.get("internetMessageId") or m["id"]
        m["_hash"] = hashlib.sha256(key.encode()).hexdigest()
        return True

    # ── payload ──────────────────────────────────────────────────────────────
    def _payload(self, msg, cid, oppid, method, conf, status, direction,
                 meaningful, snippet, mailbox, rfi):
        p, ch = self.cfg.prefix, self.cfg.choices
        key = msg.get("internetMessageId") or msg["id"]
        body = {
            f"{p}name": (msg.get("subject") or "(no subject)")[:200],
            f"{p}channel": ch.channel["Email"],
            f"{p}direction": ch.direction[direction],
            f"{p}timestamputc": msg["_ts"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            f"{p}sender": msg["_sender"][:320],
            f"{p}participants": json.dumps(msg["_participants"])[:4000],
            f"{p}snippet": snippet[:2000],
            f"{p}conversationid": (msg.get("conversationId") or "")[:512],
            f"{p}messagekey": key[:512],
            f"{p}messagekeyhash": msg["_hash"],
            f"{p}isinforequest": rfi.is_info_request,
            f"{p}rfistatus": ch.rfistatus["Open" if rfi.is_info_request else "NA"],
            f"{p}matchconfidence": conf,
            f"{p}matchstatus": ch.matchstatus[status],
            f"{p}ismeaningful": meaningful,
            f"{p}sourcelink": (msg.get("webLink") or "")[:2000],
            f"{p}provenance": f"{mailbox}|{self.runid}|{self.codeversion}"[:512],
            f"{p}contact@odata.bind": f"/contacts({cid})",
        }
        if method:
            body[f"{p}matchmethod"] = ch.matchmethod[method]
        if oppid:
            body[f"{p}opportunity@odata.bind"] = f"/opportunities({oppid})"
        return body

    def _diff_for_update(self, existing, payload):
        """Changed STABLE_FIELDS only. Match fields may only be upgraded to
        Confirmed, and a Manual (human) assignment is never touched."""
        p, ch, out = self.cfg.prefix, self.cfg.choices, {}
        for f in STABLE_FIELDS:
            k = p + f
            if k in payload and existing.get(k) != payload[k]:
                if f == "timestamputc" and existing.get(k) and \
                        parse_ts(existing[k]) == parse_ts(payload[k]):
                    continue
                out[k] = payload[k]
        human = existing.get(f"{p}matchmethod") == ch.matchmethod["Manual"]
        upgrading = (payload.get(f"{p}matchstatus") == ch.matchstatus["Confirmed"]
                     and existing.get(f"{p}matchstatus") != ch.matchstatus["Confirmed"])
        if upgrading and not human:
            for f in ("matchstatus", "matchmethod", "matchconfidence"):
                if p + f in payload:
                    out[p + f] = payload[p + f]
            if f"{p}opportunity@odata.bind" in payload:
                out[f"{p}opportunity@odata.bind"] = payload[f"{p}opportunity@odata.bind"]
        return out

    # ── per-message handling ─────────────────────────────────────────────────
    def _handle(self, msg, opp_meta, email_map, conv_map, enrich_cache):
        cfg, ch, p, c = self.cfg, self.cfg.choices, self.cfg.prefix, self.counts
        subject, preview = msg.get("subject") or "", msg.get("bodyPreview") or ""

        if matching.is_excluded(msg["_sender"], subject, preview, cfg.rules):
            c["excluded"] += 1
            return  # write_excluded=false → write nothing (config-driven)

        contacts = matching.match_contacts(msg["_participants"], email_map,
                                           cfg.org_domains)
        if not contacts:
            c["no_contact"] += 1
            return

        direction = matching.classify_direction(msg["_sender"], msg["_recipients"],
                                                cfg.org_domains)
        meaningful = not matching.looks_autoreply(subject, msg["_sender"], cfg.rules)
        conv = msg.get("conversationId") or ""

        # Enrichment GET — matched messages only, apply mode only (spec step 6).
        snippet, rfi = preview, classify(subject, preview)
        if self.apply and meaningful:
            if msg["id"] not in enrich_cache:
                enrich_cache[msg["id"]] = self.graph.enrich(msg["_mailbox"], msg["id"])
            enr = enrich_cache[msg["id"]]
            if matching.headers_autoreply(enr["headers"]):
                meaningful = False
            body = enr["body"] or preview
            snippet = body[:2000]
            rfi = classify(subject, body[:4000])   # body transient, never persisted

        for cid, oppids in contacts.items():
            oppid, method, conf, status = matching.resolve_opportunity(
                msg["_ts"], subject, snippet, oppids, opp_meta, conv_map.get(conv))
            payload = self._payload(msg, cid, oppid, method, conf, status, direction,
                                    meaningful, snippet, msg["_mailbox"], rfi)
            existing = self.dv.get_signal(msg["_hash"], cid)
            desc = f"signal {msg['_hash'][:12]}…/{cid[:8]} [{direction}/{status}]"
            if existing is None:
                created = self.dv.create(f"{p}engagementsignals", payload, desc)
                c["creates"] += 1
                if len(self.samples) < 5:
                    self.samples.append(payload)
                if rfi.is_info_request and direction == "Inbound" and created:
                    self._create_rfi(created, msg, rfi, cid, oppid)
            else:
                delta = self._diff_for_update(existing, payload)
                if delta:
                    self.dv.patch(f"{p}engagementsignals",
                                  existing[f"{p}engagementsignalid"], delta, desc)
                    c["patches"] += 1
                else:
                    c["unchanged"] += 1
            if status == "Confirmed" and oppid and conv:
                conv_map[conv] = oppid    # thread inheritance within this run
            if conv:
                self.touched_convs.add(conv)

    def _create_rfi(self, signal_row, msg, rfi, cid, oppid):
        p, ch, cfg = self.cfg.prefix, self.cfg.choices, self.cfg
        body = {
            f"{p}name": (msg.get("subject") or "(no subject)")[:200],
            f"{p}status": ch.req_status["New"],
            f"{p}category": ch.req_category.get(rfi.category or "Other",
                                                ch.req_category["Other"]),
            f"{p}receiveddate": msg["_ts"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            f"{p}duedate": add_business_days(msg["_ts"], cfg.rfi_due_bdays)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            f"{p}aigenerated": True,
            f"{p}humanconfirmed": False,
            f"{p}contact@odata.bind": f"/contacts({cid})",
            f"{p}sourcesignal@odata.bind":
                f"/{p}engagementsignals({signal_row[f'{p}engagementsignalid']})",
        }
        if oppid:
            body[f"{p}opportunity@odata.bind"] = f"/opportunities({oppid})"
        self.dv.create(f"{p}inforequests", body, f"inforequest for {msg['_hash'][:12]}…")
        self.counts["rfi_created"] += 1

    # ── post passes (apply only) ─────────────────────────────────────────────
    def _latency_and_answered_pass(self):
        p, ch = self.cfg.prefix, self.cfg.choices
        rev = ch.rev_direction
        for conv in sorted(self.touched_convs):
            rows = self.dv.conversation_signals(conv)
            sigs = [{"id": r[f"{p}engagementsignalid"],
                     "direction": rev.get(r.get(f"{p}direction"), "Internal"),
                     "ts": parse_ts(r[f"{p}timestamputc"]),
                     "latency": r.get(f"{p}responselatencymin"),
                     "rfistatus": r.get(f"{p}rfistatus")} for r in rows
                    if r.get(f"{p}timestamputc")]
            for sid, minutes in compute_latencies(sigs).items():
                self.dv.patch(f"{p}engagementsignals", sid,
                              {f"{p}responselatencymin": minutes}, f"latency {sid[:8]}")
                self.counts["latency_patched"] += 1
            # Answered flip: Open inbound with a later outbound reply
            last_out = max((s["ts"] for s in sigs if s["direction"] == "Outbound"),
                           default=None)
            if last_out:
                open_ids = [s["id"] for s in sigs
                            if s["rfistatus"] == ch.rfistatus["Open"]
                            and s["direction"] == "Inbound" and s["ts"] < last_out]
                for sid in open_ids:
                    self.dv.patch(f"{p}engagementsignals", sid,
                                  {f"{p}rfistatus": ch.rfistatus["Answered"]},
                                  f"answered {sid[:8]}")
                    self.counts["answered"] += 1
                if open_ids:
                    open_vals = [ch.req_status["New"], ch.req_status["InProgress"]]
                    for req in self.dv.requests_for_signals(open_ids, open_vals):
                        self.dv.patch(f"{p}inforequests", req[f"{p}inforequestid"],
                                      {f"{p}status":
                                       ch.req_status[self.cfg.rfi_reply_status]},
                                      f"request {req[f'{p}inforequestid'][:8]}")

    # ── run ──────────────────────────────────────────────────────────────────
    def run(self, mailboxes=None, folders=FOLDERS) -> dict:
        cfg, c = self.cfg, self.counts

        roles_by_id = None
        if cfg.rules.connection_roles:
            roles_by_id = {r["connectionroleid"]: r.get("name", "")
                           for r in self.dv.fetch_connection_roles()}
        opp_rows = self.dv.fetch_opportunities(cfg.fund_lookup)
        conn_rows = self.dv.fetch_connections()
        contact_ids = {o.get("_parentcontactid_value") for o in opp_rows}
        for cn in conn_rows:
            contact_ids.add(cn["_record1id_value"] if cn.get("record1objecttypecode") == 2
                            else cn["_record2id_value"])
        contact_rows = self.dv.fetch_contacts([i for i in contact_ids if i])
        opp_meta, email_map = build_scope(opp_rows, conn_rows, contact_rows,
                                          roles_by_id, cfg.rules.connection_roles)
        c["scope"] = {"opportunities": len(opp_meta), "contacts": len(contact_rows),
                      "emails": len(email_map)}

        messages, delta_links = [], {}
        for mb in (mailboxes or cfg.mailboxes):
            for folder in folders:
                msgs, link, resynced = self.graph.delta_messages(
                    mb, folder, self._load_delta(mb, folder))
                delta_links[(mb, folder)] = link
                c["mailboxes"][f"{mb}/{folder}"] = {"fetched": len(msgs),
                                                    "resynced": resynced}
                for m in msgs:
                    m["_mailbox"], m["_folder"] = mb, folder
                    if self._prep(m):
                        messages.append(m)
                    else:
                        c["below_floor"] += 1

        messages.sort(key=lambda m: m["_ts"])   # thread inheritance needs order
        conv_map = self.dv.confirmed_conv_opps(
            [m.get("conversationId") for m in messages],
            cfg.choices.matchstatus["Confirmed"])

        enrich_cache = {}
        for m in messages:
            self._handle(m, opp_meta, email_map, conv_map, enrich_cache)

        if self.apply:
            self._latency_and_answered_pass()
            for (mb, folder), link in delta_links.items():
                self._save_delta(mb, folder, link)

        log = {"runid": self.runid, "apply": self.apply,
               "codeversion": self.codeversion, "counts": c,
               "samples": self.samples, "dv_intents": len(self.dv.intents)}
        runs = self.state_dir / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        (runs / f"{self.runid}.json").write_text(json.dumps(log, indent=2, default=str))
        return log


def main():
    from .dataverse_client import DataverseClient
    from .graph_client import GraphClient

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--mailbox", action="append", help="limit to specific mailbox(es)")
    args = ap.parse_args()

    cfg = Config.from_env()
    graph = GraphClient(cfg.tenant_id, cfg.client_id, cfg.client_secret)
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, cfg.prefix, apply=args.apply)
    run = SyncRun(cfg, graph, dv, apply=args.apply, codeversion=code_version())
    log = run.run(mailboxes=args.mailbox)

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[{mode}] run {log['runid']} — counts:")
    print(json.dumps(log["counts"], indent=2, default=str))
    if not args.apply:
        print(f"\n{len(dv.intents)} intended writes (first 10):")
        for i in dv.intents[:10]:
            print(f"  {i}")
        print(f"\nRun log: ingestion/state/runs/{log['runid']}.json")


if __name__ == "__main__":
    main()
