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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .bizhours import WORKDAYS
from .classify import RfiResult, classify
from .config import Config, STATE_DIR
from .latency import compute_response_pairs
from . import llm, matching, noise

# classify.py urgency labels → Choices.urgency keys
URGENCY_KEY = {"none": "None", "urgent_language": "UrgentLanguage",
               "explicit_deadline": "ExplicitDeadline"}

# Categories that exist only in the v2 option set (DEV 2026-08-04). In an env
# that predates the solution import they fold to Other — writing their option
# values would 400. Same probe as the v2 columns: both land in one import.
V2_ONLY_CATEGORIES = {"CapitalCall", "TaxDocs", "AccountAdmin", "LiquidityTransfer"}

# Phase-0 recommendation layer (claude_ir.md §4). Stamped into new_categoryprovenance
# so a later reclassification on a new taxonomy/prompt version is detectable and the
# AI suggestion is auditable. Bump when the classifier prompt or taxonomy changes.
RECO_PROMPT_VERSION = "ir-cat-v4-2026-08-09"

FOLDERS = ("inbox", "sentitems")

# Stable message facts that may be corrected on re-sync. Volatile/owned-elsewhere
# fields (provenance, rfistatus, isinforequest, responselatencymin) are set at
# create time only — comparing them would cause perpetual re-patches and stomp
# later-pass / human updates (no-silent-mutation, house rule 2).
STABLE_FIELDS = ("name", "sender", "timestamputc", "conversationid", "sourcelink",
                 "participants", "direction", "snippet", "messagekey", "ismeaningful")


def clip(s: str, limit: int) -> str:
    """Truncate to `limit` UTF-16 code units. Dataverse measures string length
    like .NET — emoji/astral chars count as 2 — so a Python [:N] slice can
    still overflow the column (found live: 'new_snippet exceeded 2000')."""
    if not s:
        return s
    b = s.encode("utf-16-le")
    if len(b) <= limit * 2:
        return s
    return b[: limit * 2].decode("utf-16-le", "ignore")


def say(msg: str):
    """Timestamped, flushed progress line — visible in the log while running."""
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def parse_ts(s: str) -> datetime:
    """Always tz-aware: DateOnly columns (e.g. monitoringstartdate) come back
    as bare dates — treat them as UTC midnight, or comparisons explode."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sane_deadline(deadline_iso: str | None, received: datetime) -> str | None:
    """The classifier sometimes emits the wrong YEAR for 'by 8/15'-style
    deadlines (found live: 2025-08-15 on a 2026-07-31 email). A deadline
    before receipt gets bumped one year; still-implausible values are
    dropped rather than stored."""
    if not deadline_iso:
        return None
    try:
        d = parse_ts(deadline_iso)
    except ValueError:
        return None
    if d >= received:
        return d.date().isoformat()
    bumped = d.replace(year=d.year + 1)
    if bumped >= received:
        return bumped.date().isoformat()
    return None


def add_business_days(d: datetime, n: int) -> datetime:
    """Sun–Thu work week — the same convention as bizhours.business_minutes."""
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() in WORKDAYS:
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
            "prospectcode": (o.get("new_prospectcode") or "").strip(),
            "live": bool(o.get("new_live")),   # v2 matcher scope (WS1)
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
        self.error_samples = []
        self.touched_convs = set()
        self.new_signal_rfis = []   # (signal_id, msg, category)
        self._ircat_map = None      # {category code: new_ircategory rowid}, lazy/cached
        self._routing = None        # (route_by_category, excluded_emails), lazy/cached
        self._user_by_email = {}    # email -> (uid, email, name) | None  (active internal)
        self._user_by_id = {}       # uid   -> (uid, email, name) | None  (active internal)
        self._intake_cache = {}     # sender email → contactid (per run)

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

    # ── category ──────────────────────────────────────────────────────────────
    def _category_value(self, cat: str | None, entity: str = "inforequest") -> int:
        """Category label → option value for `{entity}.{prefix}category`, folding
        labels the TARGET COLUMN's option set doesn't have yet (writing a missing
        option value 400s). `entity` is 'inforequest' (default) or
        'engagementsignal' — the two category columns can drift if a solution
        import lands options on one but not the other, so each write validates
        against its own column. NDA folds to Legal-SideLetter until the v3 signal
        column exists; the v2 four fold to Other in envs predating that import."""
        p, ch = self.cfg.prefix, self.cfg.choices
        cat = cat or "Other"
        if cat == "NDA" and not self.dv.has_attribute(f"{p}engagementsignal", f"{p}category"):
            cat = "Legal-SideLetter"
        if cat in V2_ONLY_CATEGORIES and not self.dv.has_attribute(f"{p}inforequest", f"{p}thirdparty"):
            cat = "Other"
        val = ch.req_category.get(cat, ch.req_category["Other"])
        # General net for appended options (v4+): never write an option value the
        # target column's option set lacks — fold to Other until the import lands.
        valid = self.dv.category_option_values(f"{p}{entity}", f"{p}category")
        if valid is not None and val not in valid:
            return ch.req_category["Other"]
        return val

    # ── payload ──────────────────────────────────────────────────────────────
    def _payload(self, msg, cid, oppid, method, conf, status, direction,
                 meaningful, snippet, mailbox, rfi, noise_reason=None,
                 is_primary=True):
        p, ch = self.cfg.prefix, self.cfg.choices
        key = msg.get("internetMessageId") or msg["id"]
        body = {
            # §0.1: one primary attribution per (opportunity, message); set at
            # create time, never re-patched (backfill_close_columns repairs)
            f"{p}isprimary": is_primary,
            f"{p}name": clip(msg.get("subject") or "(no subject)", 200),
            f"{p}channel": ch.channel["Email"],
            f"{p}direction": ch.direction[direction],
            f"{p}timestamputc": msg["_ts"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            f"{p}sender": clip(msg["_sender"], 320),
            f"{p}participants": clip(json.dumps(msg["_participants"]), 4000),
            f"{p}snippet": clip(snippet, 2000),
            f"{p}conversationid": clip(msg.get("conversationId") or "", 512),
            f"{p}messagekey": clip(key, 512),
            f"{p}messagekeyhash": msg["_hash"],
            f"{p}isinforequest": rfi.is_info_request,
            f"{p}rfistatus": ch.rfistatus["Open" if rfi.is_info_request else "NA"],
            f"{p}matchconfidence": conf,
            f"{p}matchstatus": ch.matchstatus[status],
            f"{p}ismeaningful": meaningful,
            f"{p}sourcelink": clip(msg.get("webLink") or "", 2000),
            f"{p}provenance": clip(f"{mailbox}|{self.runid}|{self.codeversion}", 512),
            f"{p}contact@odata.bind": f"/contacts({cid})",
        }
        if method:
            body[f"{p}matchmethod"] = ch.matchmethod[method]
        if oppid:
            body[f"{p}opportunity@odata.bind"] = f"/opportunities({oppid})"
        if noise_reason:
            body[f"{p}noisereason"] = clip(noise_reason, 100)
        # v3: tag the signal itself with the LLM category (ir@ route only).
        # Set at create time; inert until the new_category column exists in the
        # target env (probe cached per run), so it never 400s in PROD pre-import.
        if self.cfg.tag_signal_category and rfi.category \
                and self.dv.has_attribute(f"{p}engagementsignal", f"{p}category"):
            body[f"{p}category"] = self._category_value(rfi.category,
                                                        entity="engagementsignal")
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

    # ── per-message handling (v2: noise gate → boosters → thresholds) ────────
    def _handle(self, msg, opp_meta, email_map, conv_map, enrich_cache,
                regarding_map=None, noise_verdicts=None):
        cfg, ch, p, c = self.cfg, self.cfg.choices, self.cfg.prefix, self.counts
        subject, preview = msg.get("subject") or "", msg.get("bodyPreview") or ""

        if matching.is_excluded(msg["_sender"], subject, preview, cfg.rules):
            c["excluded"] += 1
            return  # write_excluded=false → write nothing (config-driven)

        direction = matching.classify_direction(msg["_sender"], msg["_recipients"],
                                                cfg.org_domains)
        meaningful = not matching.looks_autoreply(subject, msg["_sender"], cfg.rules)
        conv = msg.get("conversationId") or ""
        key = msg.get("internetMessageId") or msg["id"]

        # Noise gate (WS1): heuristic verdict precomputed in run(); LLM verdicts
        # for candidates arrive via noise_verdicts. Resolved BEFORE the contact
        # check so intake never creates contacts for noise senders.
        noise_reason = msg.get("_noise_reason")
        if noise_reason is None and noise_verdicts and msg["id"] in noise_verdicts:
            noise_reason = noise.llm_label_to_noise_reason(noise_verdicts[msg["id"]])

        contacts = msg["_contacts"]   # precomputed in run()
        if not contacts and msg.get("_intake") and direction == "Inbound" \
                and meaningful and not noise_reason:
            # ir@ intake (docs/ir-intake-design.md): unknown human sender to an
            # intake mailbox → find-or-create a lightweight contact. In dry-run
            # a to-be-created contact has no id yet — intent is recorded and
            # the message is skipped until --apply.
            cid = self._intake_contact(msg)
            if cid:
                contacts = {cid: set()}
        if not contacts:
            c["no_contact"] += 1
            return

        # Enrichment GET — matched, non-noise messages only, apply mode only.
        # The (LLM) request classifier runs ONLY on the enriched body below —
        # never on previews, never in dry runs (cost control, WS6).
        snippet, rfi = preview, RfiResult()
        if self.apply and meaningful and not noise_reason:
            if msg["id"] not in enrich_cache:
                enrich_cache[msg["id"]] = self.graph.enrich(msg["_mailbox"], msg["id"])
            enr = enrich_cache[msg["id"]]
            hn = noise.header_noise(enr["headers"])   # List-Unsubscribe etc.
            if hn:
                noise_reason = hn
            elif matching.headers_autoreply(enr["headers"]):
                meaningful = False
            body = enr["body"] or preview
            snippet = body[:2000]
            if not noise_reason and direction == "Inbound":
                # WS6: LLM request detection on confirmed-bound inbound only
                rfi = classify(subject, body[:4000])  # body transient, never persisted

        primary_groups = set()   # §0.1: first attribution per opp is primary
        rfi_done = False         # ONE request per message, not per attribution
        for cid, oppids in contacts.items():
            if noise_reason:
                oppid, method, conf, status = None, None, 0, "Excluded"
            else:
                oppid, method, conf = matching.resolve_opportunity_v2(
                    subject, snippet, oppids, opp_meta, conv_map.get(conv),
                    regarding_opp=(regarding_map or {}).get(key))
                dispo = matching.disposition_for(conf, cfg)
                if dispo == "auto_confirmed":
                    status = "Confirmed"
                elif dispo == "needs_review":
                    status = "Suggested" if oppid else "Unmatched"
                else:                       # < review_min → noise (spec 1.3)
                    status, noise_reason = "Excluded", "low_confidence"
                    oppid, method = (oppid, None)  # keep top candidate for audit
                if msg.get("_intake") and noise_reason == "low_confidence":
                    # intake-mailbox mail must reach the review queue, never
                    # low-confidence noise (docs/ir-intake-design.md) — the
                    # whole point is visibility of servicing traffic
                    status = "Suggested" if oppid else "Unmatched"
                    noise_reason = None
            c.setdefault("by_status", {}).setdefault(status, 0)
            c["by_status"][status] += 1
            c.setdefault("by_method", {}).setdefault(method or "none", 0)
            c["by_method"][method or "none"] += 1
            if noise_reason:
                c.setdefault("noise_by_reason", {}).setdefault(noise_reason, 0)
                c["noise_by_reason"][noise_reason] += 1
            if oppid and opp_meta.get(oppid, {}).get("fund_name"):
                fn = opp_meta[oppid]["fund_name"]
                c.setdefault("by_fund", {}).setdefault(fn, 0)
                c["by_fund"][fn] += 1
            grp = oppid or f"c:{cid}"        # no-opp rows stay primary per contact
            is_primary = grp not in primary_groups
            primary_groups.add(grp)
            payload = self._payload(msg, cid, oppid, method, conf, status, direction,
                                    meaningful and not noise_reason, snippet,
                                    msg["_mailbox"], rfi, noise_reason=noise_reason,
                                    is_primary=is_primary)
            existing = self.dv.get_signal(msg["_hash"], cid)
            desc = f"signal {msg['_hash'][:12]}…/{cid[:8]} [{direction}/{status}]"
            if existing is None:
                created = self.dv.create(f"{p}engagementsignals", payload, desc)
                c["creates"] += 1
                if len(self.samples) < 5:
                    self.samples.append(payload)
                third_party_skip = (self.cfg.suppress_third_party_requests
                                     and rfi.third_party)
                if self.cfg.create_requests and rfi.is_info_request \
                        and direction == "Inbound" and created \
                        and not noise_reason and not rfi_done \
                        and not third_party_skip:
                    # one ticket per email — the same message matched to N
                    # contacts must not open N requests (dup-ticket fix).
                    # Option B: only the restricted IR-request route creates
                    # requests (cfg.create_requests); the broad sync = signals only.
                    # 3rd-party (advisor/bank/custodian) senders log a signal but
                    # open no ticket — the feed stays investor inquiries only.
                    self._create_rfi(created, msg, rfi, cid, oppid)
                    rfi_done = True
                elif third_party_skip and rfi.is_info_request and created \
                        and direction == "Inbound" and not noise_reason:
                    self.counts.setdefault("rfi_skipped_thirdparty", 0)
                    self.counts["rfi_skipped_thirdparty"] += 1
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
            if conv and not noise_reason:
                self.touched_convs.add(conv)

    def _intake_contact(self, msg) -> str | None:
        """Find-or-create a lightweight contact for an unknown intake-mailbox
        sender (docs/ir-intake-design.md). Reuse-before-create: ANY existing
        CRM contact with the address wins. Returns contactid, or None in
        dry-run when the contact would need creating (intent recorded)."""
        p, sender = self.cfg.prefix, msg["_sender"]
        if sender in self._intake_cache:
            return self._intake_cache[sender]
        row = self.dv.find_contact_by_email(sender)
        if row:
            cid = row["contactid"]
        else:
            name = (((msg.get("from") or {}).get("emailAddress") or {})
                    .get("name") or "").strip()
            if not name or "@" in name:   # display name absent or is the address
                name = sender.split("@")[0].replace(".", " ").title()
            first, _, last = name.partition(" ")
            body = {"firstname": first[:50], "emailaddress1": sender,
                    f"{p}autocreatedby": f"ir-intake|{self.runid}"}
            if last.strip():
                body["lastname"] = last.strip()[:50]
            created = self.dv.create("contacts", body, f"intake contact {sender}")
            self.counts["intake_contacts"] = self.counts.get("intake_contacts", 0) + 1
            if not created:
                return None   # dry-run: no id to link yet
            cid = created["contactid"]
        self._intake_cache[sender] = cid
        return cid

    # ── Phase-0 recommendation layer (claude_ir.md §4) ─────────────────────────
    def _ircategory_map(self) -> dict:
        """{category code → new_ircategory rowid} for ACTIVE reference rows, cached
        per run. Empty {} when the reference table is unreadable (absent in this
        env, or PROD before its row-level privileges are granted — a 403). The
        recommendation layer then no-ops without ever breaking request creation."""
        if self._ircat_map is None:
            p = self.cfg.prefix
            try:
                rows = self.dv.query(
                    f"{p}ircategories?$select={p}name,{p}ircategoryid"
                    f"&$filter={p}active eq true")
                self._ircat_map = {r[f"{p}name"]: r[f"{p}ircategoryid"]
                                   for r in rows if r.get(f"{p}name")}
                say(f"reference taxonomy: {len(self._ircat_map)} active categories")
            except Exception as e:
                self._ircat_map = {}
                say(f"reference taxonomy unreadable ({str(e)[:80]}) — "
                    f"category recommendation disabled this run")
        return self._ircat_map

    def _recommendation_fields(self, rfi, msg, signal_row) -> dict:
        """AI category written as a RECOMMENDATION against the versioned reference
        table (new_ircategory): lookup + confidence + provenance. NEVER *actual (a
        human promotes that). Inert where the columns/table are absent, so it is a
        no-op in envs predating the Phase-0 import — same guard style as the v2
        block below."""
        p = self.cfg.prefix
        if not rfi.category:
            return {}
        if not self.dv.has_attribute(f"{p}inforequest", f"{p}categoryrecommended"):
            return {}
        rowid = self._ircategory_map().get(rfi.category)
        if not rowid:                      # unknown code / table unreadable → skip
            return {}
        prov = json.dumps(
            {"model": llm.REQUEST_MODEL, "prompt_ver": RECO_PROMPT_VERSION,
             "ts": msg["_ts"].strftime("%Y-%m-%dT%H:%M:%SZ"),
             "signal_ids": [signal_row[f"{p}engagementsignalid"]]},
            separators=(",", ":"))
        return {
            f"{p}categoryrecommended@odata.bind": f"/{p}ircategories({rowid})",
            f"{p}categoryconfidence": rfi.confidence,
            f"{p}categoryprovenance": clip(prov, 2000),
        }

    def _routing_maps(self):
        """(route_by_category, excluded_emails) from ACTIVE new_irrule rows, cached
        per run. route_by_category[code] = {uid, name, email, tier, rule}; earlier
        priority wins; a DISABLED handler (leaver) is skipped. Empty on any read failure (rules absent / PROD pre-privilege)
        so assignee recommendation simply no-ops."""
        if self._routing is None:
            p = self.cfg.prefix
            FMT = "@OData.Community.Display.V1.FormattedValue"
            route, excl = {}, set()
            try:
                rows = self.dv.query(
                    f"{p}irrules?$select={p}name,{p}matchtype,{p}matchvalue,{p}action,"
                    f"{p}tier&$expand={p}handler($select=systemuserid,fullname,"
                    f"internalemailaddress,isdisabled)&$filter={p}active eq true"
                    f"&$orderby={p}priority")
                for r in rows:
                    mt, ac = r.get(f"{p}matchtype{FMT}"), r.get(f"{p}action{FMT}")
                    mv = (r.get(f"{p}matchvalue") or "").strip()
                    if ac == "ExcludeHandler" and mt == "Handler" and mv:
                        excl.add(mv.lower())
                    elif ac == "Route" and mt == "Category" and mv and mv not in route:
                        h = r.get(f"{p}handler") or {}
                        if h.get("systemuserid") and not h.get("isdisabled"):
                            route[mv] = {"uid": h["systemuserid"],
                                         "name": h.get("fullname") or "",
                                         "email": (h.get("internalemailaddress") or "").lower(),
                                         "tier": r.get(f"{p}tier{FMT}"),
                                         "rule": r.get(f"{p}name")}
                say(f"routing rules: {len(route)} category routes, {len(excl)} excluded")
            except Exception as e:
                say(f"routing rules unreadable ({str(e)[:80]}) — assignee rec disabled")
            self._routing = (route, excl)
        return self._routing

    def _resolve_user(self, *, uid=None, email=None):
        """(uid, email, name) for an ACTIVE internal systemuser, else None. Cached.
        Resolve by GUID or by primary email; disabled users resolve to None."""
        if uid:
            if uid not in self._user_by_id:
                rows = self.dv.query(
                    f"systemusers?$select=systemuserid,internalemailaddress,fullname,"
                    f"isdisabled&$filter=systemuserid eq {uid}")
                r = rows[0] if rows else None
                self._user_by_id[uid] = (
                    (uid, (r.get("internalemailaddress") or "").lower(), r.get("fullname") or "")
                    if r and not r.get("isdisabled") else None)
            return self._user_by_id[uid]
        email = (email or "").lower()
        if not email:
            return None
        if email not in self._user_by_email:
            rows = self.dv.query(
                f"systemusers?$select=systemuserid,internalemailaddress,fullname"
                f"&$filter=internalemailaddress eq '{email}' and isdisabled eq false")
            self._user_by_email[email] = (
                (rows[0]["systemuserid"], email, rows[0].get("fullname") or "")
                if rows else None)
        return self._user_by_email[email]

    def _last_responder(self, cid):
        """§4.2 #2: the internal user who most recently responded (outbound) to this
        contact. Skips senders that aren't resolvable internal users (e.g. the shared
        ir@ mailbox)."""
        p, ch = self.cfg.prefix, self.cfg.choices
        rows = self.dv.query(
            f"{p}engagementsignals?$select={p}sender,{p}timestamputc"
            f"&$filter=_{p}contact_value eq {cid} and {p}direction eq {ch.direction['Outbound']}"
            f"&$orderby={p}timestamputc desc&$top=8")
        for r in rows:
            u = self._resolve_user(email=r.get(f"{p}sender"))
            if u:
                return u
        return None

    def _relationship_owner(self, cid):
        """§4.2 #3: the contact's owner, when it is a user (not a team)."""
        p = self.cfg.prefix
        rows = self.dv.query(f"contacts?$select=_ownerid_value&$filter=contactid eq {cid}")
        if rows and rows[0].get(
                "_ownerid_value@Microsoft.Dynamics.CRM.lookuplogicalname") == "systemuser":
            return self._resolve_user(uid=rows[0]["_ownerid_value"])
        return None

    def _assignee_fields(self, rfi, msg, cid=None) -> dict:
        """§4.2: recommend an assignee by the weighted inputs — (1) explicit routing
        rule, (2) last Exigent responder to the contact, (3) relationship owner —
        taking the highest-weight non-excluded active user. Recommendation-only:
        writes assigneerecommended (lookup) + reason + provenance, NEVER ownerid /
        *actual (a human 'Take it' / 'Assign to' does that). Inert where absent."""
        p = self.cfg.prefix
        if not self.dv.has_attribute(f"{p}inforequest", f"{p}assigneerecommended"):
            return {}
        route, excl = self._routing_maps()
        candidates = []                                  # (uid, email, name, source, basis)
        hit = route.get(rfi.category) if rfi.category else None
        if hit:
            candidates.append((hit["uid"], hit["email"], hit["name"], "routing-rule",
                               f"routing rule '{hit['rule']}' for category {rfi.category}"))
        if cid:
            for finder, source, basis in (
                (self._last_responder, "last-responder",
                 "most recent Exigent responder to this contact"),
                (self._relationship_owner, "relationship-owner",
                 "the contact's relationship owner")):
                try:
                    u = finder(cid)
                except Exception:
                    u = None
                if u:
                    candidates.append((u[0], u[1], u[2], source, basis))
        for uid, email, name, source, basis in candidates:
            if email in excl:                            # never recommend a leaving user
                continue
            prov = json.dumps({"source": source, "basis": basis, "category": rfi.category,
                               "considered": [c[3] for c in candidates],
                               "ts": msg["_ts"].strftime("%Y-%m-%dT%H:%M:%SZ")},
                              separators=(",", ":"))
            return {
                f"{p}assigneerecommended@odata.bind": f"/systemusers({uid})",
                f"{p}assigneereason": clip(f"Recommended {name or email} — {basis}.", 500),
                f"{p}assigneeprovenance": clip(prov, 2000),
            }
        return {}

    def _infer_status(self, conv_id, now=None):
        """§4.5: (statusinferred label, provenance JSON) from the thread's traffic —
        AwaitingInvestor (we replied last), AwaitingInternal (they replied last), or
        PossiblyClosable (no traffic for status_closable_days after our reply).
        (None, None) with no usable conversation. NEVER closes a request."""
        if not conv_id:
            return None, None
        p, ch = self.cfg.prefix, self.cfg.choices
        rev = ch.rev_direction
        sigs = [s for s in self.dv.conversation_signals(conv_id)
                if s.get(f"{p}ismeaningful") and s.get(f"{p}timestamputc")]

        def last(name):
            ts = [s[f"{p}timestamputc"] for s in sigs
                  if rev.get(s.get(f"{p}direction")) == name]
            return max(ts) if ts else None

        last_in, last_out = last("Inbound"), last("Outbound")
        if not last_in and not last_out:
            return None, None
        now = now or datetime.now(timezone.utc)
        parse = lambda t: datetime.fromisoformat(t.replace("Z", "+00:00"))
        if last_out and (not last_in or parse(last_out) >= parse(last_in)):
            days = (now - parse(last_out)).days
            if days >= self.cfg.status_closable_days:
                label, rule = "PossiblyClosable", f"no traffic {days}d after our last reply"
            else:
                label, rule = "AwaitingInvestor", "we replied after their last message"
        else:
            label, rule = "AwaitingInternal", "they replied after our last message"
        prov = json.dumps({"rule": rule, "last_inbound": last_in, "last_outbound": last_out,
                           "closable_days": self.cfg.status_closable_days,
                           "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ")}, separators=(",", ":"))
        return label, prov

    def _status_fields(self, conv_id) -> dict:
        """§4.5 write map: new_statusinferred + provenance. NEVER new_status (the
        human's status_actual). Inert where the column is absent."""
        p = self.cfg.prefix
        if not self.dv.has_attribute(f"{p}inforequest", f"{p}statusinferred"):
            return {}
        label, prov = self._infer_status(conv_id)
        if not label:
            return {}
        return {f"{p}statusinferred": self.cfg.choices.statusinferred[label],
                f"{p}statusprovenance": clip(prov, 2000)}

    def _create_rfi(self, signal_row, msg, rfi, cid, oppid):
        p, ch, cfg = self.cfg.prefix, self.cfg.choices, self.cfg
        # request title = the LLM's one-sentence description when present
        title = rfi.description or msg.get("subject") or "(no subject)"
        # explicit deadline: LLM ISO date, sanity-checked against receipt
        # (wrong-year emissions get bumped or dropped — issue found live)
        deadline_iso = sane_deadline(rfi.deadline, msg["_ts"])
        due = (parse_ts(deadline_iso) if deadline_iso
               else add_business_days(msg["_ts"], cfg.rfi_due_bdays))
        has_v2 = self.dv.has_attribute(f"{p}inforequest", f"{p}thirdparty")
        body = {
            f"{p}name": clip(title, 200),
            f"{p}status": ch.req_status["New"],
            f"{p}category": self._category_value(rfi.category),
            f"{p}receiveddate": msg["_ts"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            f"{p}duedate": due.strftime("%Y-%m-%dT%H:%M:%SZ"),
            f"{p}statedurgency": ch.urgency[URGENCY_KEY.get(rfi.urgency or "none",
                                                            "None")],
            f"{p}aigenerated": True,
            f"{p}humanconfirmed": False,
            f"{p}contact@odata.bind": f"/contacts({cid})",
            f"{p}sourcesignal@odata.bind":
                f"/{p}engagementsignals({signal_row[f'{p}engagementsignalid']})",
        }
        if deadline_iso:
            body[f"{p}explicitdeadline"] = deadline_iso
        if oppid:
            body[f"{p}opportunity@odata.bind"] = f"/opportunities({oppid})"
        # close-readiness §2.2: routing column (PROD since 2026-08-03 import)
        if rfi.routing:
            body[f"{p}routingcategory"] = ch.routing[rfi.routing]
        # v2 taxonomy fields — provisioned to DEV 2026-08-04; written only once
        # the columns exist in the target env (PROD gets them at the next
        # manual solution import — same pattern as the §0.1 columns).
        if has_v2:
            body[f"{p}thirdparty"] = rfi.third_party
            body[f"{p}classifierconfidence"] = rfi.confidence
            if rfi.secondary and rfi.secondary in ch.req_category:
                body[f"{p}secondarycategory"] = self._category_value(rfi.secondary)
        # Phase-0: AI category recommendation (lookup + confidence + provenance).
        body.update(self._recommendation_fields(rfi, msg, signal_row))
        # Phase-2 (§4.2): assignee recommendation — routing rule, else last responder
        # / relationship owner (lookup + reason + provenance).
        body.update(self._assignee_fields(rfi, msg, cid))
        self.dv.create(f"{p}inforequests", body, f"inforequest for {msg['_hash'][:12]}…")
        self.counts["rfi_created"] += 1

    # ── post passes (apply only) ─────────────────────────────────────────────
    def _latency_and_answered_pass(self):
        p, ch = self.cfg.prefix, self.cfg.choices
        rev = ch.rev_direction
        excluded_val = ch.matchstatus["Excluded"]
        for conv in sorted(self.touched_convs):
            rows = self.dv.conversation_signals(conv)
            # noise / auto-reply rows never participate in reply pairing (WS1/WS3)
            rows = [r for r in rows if r.get(f"{p}ismeaningful")
                    and r.get(f"{p}matchstatus") != excluded_val]
            sigs = [{"id": r[f"{p}engagementsignalid"],
                     "contact": r.get(f"_{p}contact_value"),
                     "direction": rev.get(r.get(f"{p}direction"), "Internal"),
                     "ts": parse_ts(r[f"{p}timestamputc"]),
                     "latency": r.get(f"{p}responselatencymin"),
                     "meaningful": True,   # rows pre-filtered above
                     "rfistatus": r.get(f"{p}rfistatus")} for r in rows
                    if r.get(f"{p}timestamputc")]
            # WS3: inbound-anchored pairing, business-minute latency
            for sid, minutes in compute_response_pairs(sigs).items():
                self.dv.patch(f"{p}engagementsignals", sid,
                              {f"{p}responselatencymin": minutes}, f"latency {sid[:8]}")
                self.counts["latency_patched"] += 1
            # Answered flip: Open inbound with a later outbound reply. The
            # matching request gets a completed timestamp (the answering
            # outbound's ts) but the record stays for aging analytics.
            out_ts = sorted(s["ts"] for s in sigs if s["direction"] == "Outbound")
            if out_ts:
                answered_at = {}     # signal id → ts of earliest later outbound
                for s in sigs:
                    if s["rfistatus"] == ch.rfistatus["Open"] \
                            and s["direction"] == "Inbound":
                        reply = next((t for t in out_ts if t > s["ts"]), None)
                        if reply:
                            answered_at[s["id"]] = reply
                for sid in answered_at:
                    self.dv.patch(f"{p}engagementsignals", sid,
                                  {f"{p}rfistatus": ch.rfistatus["Answered"]},
                                  f"answered {sid[:8]}")
                    self.counts["answered"] += 1
                if answered_at:
                    open_vals = [ch.req_status["New"], ch.req_status["InProgress"]]
                    for req in self.dv.requests_for_signals(list(answered_at),
                                                            open_vals):
                        done = answered_at.get(
                            req.get(f"_{p}sourcesignal_value"), out_ts[-1])
                        self.dv.patch(
                            f"{p}inforequests", req[f"{p}inforequestid"],
                            {f"{p}status": ch.req_status["Completed"],
                             f"{p}completeddate":
                                 done.strftime("%Y-%m-%dT%H:%M:%SZ")},
                            f"request {req[f'{p}inforequestid'][:8]} completed")

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

        say(f"scope: {len(opp_meta)} opps, {len(email_map)} contact emails")
        messages, delta_links = [], {}
        pairs = [(mb, f) for mb in (mailboxes or cfg.mailboxes) for f in folders]

        def walk(pair):
            mb, folder = pair
            # one Graph client per worker — requests.Session isn't thread-safe
            g = self.graph.clone() if hasattr(self.graph, "clone") else self.graph
            return pair, g.delta_messages(mb, folder, self._load_delta(mb, folder))

        with ThreadPoolExecutor(max_workers=min(8, len(pairs))) as ex:
            for (mb, folder), (msgs, link, resynced) in ex.map(walk, pairs):
                delta_links[(mb, folder)] = link
                c["mailboxes"][f"{mb}/{folder}"] = {"fetched": len(msgs),
                                                    "resynced": resynced}
                say(f"delta {mb}/{folder}: {len(msgs)} messages")
                for m in msgs:
                    m["_mailbox"], m["_folder"] = mb, folder
                    if self._prep(m):
                        messages.append(m)
                    else:
                        c["below_floor"] += 1

        messages.sort(key=lambda m: m["_ts"])   # thread inheritance needs order
        # precompute contact matches; the conv-map query only needs conversations
        # that involve a matched contact (~2k), not every conversation (~25k)
        for m in messages:
            m["_contacts"] = matching.match_contacts(m["_participants"], email_map,
                                                     cfg.org_domains)
        # ir@ intake eligibility (docs/ir-intake-design.md): activates only when
        # the marker column exists in the target env (dormant pre-import)
        intake_on = bool(cfg.intake_mailboxes) and \
            self.dv.has_attribute("contact", f"{cfg.prefix}autocreatedby")
        if cfg.intake_mailboxes and not intake_on:
            say("intake: contact.new_autocreatedby absent in this env — dormant")
        intake_floor = cfg.intake_floor or cfg.ingest_floor
        for m in messages:
            m["_intake"] = (intake_on and m["_mailbox"] in cfg.intake_mailboxes
                            and m["_folder"] == "inbox"
                            and m["_ts"] >= intake_floor
                            and matching.domain_of(m["_sender"])
                            not in cfg.org_domains)
        convs_needed = sorted({m["conversationId"] for m in messages
                               if (m["_contacts"] or m.get("_intake"))
                               and m.get("conversationId")})
        say(f"conv-map: querying {len(convs_needed)} relevant conversations")
        conv_map = self.dv.confirmed_conv_opps(
            convs_needed, cfg.choices.matchstatus["Confirmed"])

        # WS1 booster 1: Dynamics Regarding ground truth (internetMessageId → opp)
        regarding_map = self.dv.fetch_regarding_map(
            cfg.ingest_floor.strftime("%Y-%m-%dT%H:%M:%SZ"))
        say(f"regarding-map: {len(regarding_map)} emails carry an Opportunity "
            f"Regarding; conv-map: {len(conv_map)} confirmed; "
            f"processing {len(messages)} messages")

        # WS1 noise gate — heuristics inline, LLM batch for candidates
        candidates = []
        for m in messages:
            if not (m["_contacts"] or m.get("_intake")):
                continue
            verdict, reason = noise.gate(
                m["_sender"], m.get("subject") or "", m.get("bodyPreview") or "",
                cfg.rules,
                sender_is_matched_contact=m["_sender"] in email_map,
                sender_is_internal=matching.domain_of(m["_sender"]) in cfg.org_domains)
            m["_noise_reason"] = reason if verdict == "noise" else None
            if verdict == "candidate":
                candidates.append({"key": m["id"], "sender": m["_sender"],
                                   "subject": m.get("subject") or "",
                                   "snippet": m.get("bodyPreview") or ""})
        noise_verdicts = llm.classify_noise(candidates, log=say) if candidates else {}

        enrich_cache = {}
        for i, m in enumerate(messages, 1):
            try:
                self._handle(m, opp_meta, email_map, conv_map, enrich_cache,
                             regarding_map=regarding_map,
                             noise_verdicts=noise_verdicts)
            except Exception as e:   # one bad row must never kill a 2h run
                c["errors"] = c.get("errors", 0) + 1
                self.error_samples.append(f"{m.get('id', '?')}: {e}")
                if c["errors"] <= 5:
                    say(f"ERROR on message {m.get('id', '?')[:24]}: {str(e)[:300]}")
            if i % 2000 == 0:
                say(f"processed {i}/{len(messages)} — "
                    f"{self.counts['creates']} creates so far")

        if self.apply:
            say(f"creates done ({self.counts['creates']}, "
                f"{c.get('errors', 0)} errors); "
                f"latency pass over {len(self.touched_convs)} conversations")
            self._latency_and_answered_pass()
            if c.get("errors"):
                # skipped messages would be lost forever if tokens advance —
                # leave them unsaved so the next (idempotent) run retries all
                say(f"{c['errors']} errors — delta tokens NOT advanced; "
                    "next run re-walks and retries (idempotent)")
            else:
                for (mb, folder), link in delta_links.items():
                    self._save_delta(mb, folder, link)

        log = {"runid": self.runid, "apply": self.apply,
               "codeversion": self.codeversion, "counts": c,
               "samples": self.samples, "errors": self.error_samples[:20],
               "dv_intents": len(self.dv.intents)}
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
