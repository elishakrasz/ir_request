"""Configuration for the ingestion service: .env + rules.json.

House rule 1: refuses a PROD Dataverse URL outright.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE_DIR = Path(__file__).resolve().parent / "state"


def load_env(path: Path = REPO / ".env") -> dict:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


@dataclass
class Rules:
    noise_domains: list = field(default_factory=list)
    # Tier-0 admin/platform distributions (fund-admin blasts, portal robots):
    # recorded as noise (reason admin_blast) — never classified, never a ticket.
    bulk_senders: list = field(default_factory=list)
    bulk_sender_domains: list = field(default_factory=list)
    excluded_senders: list = field(default_factory=list)
    excluded_domains: list = field(default_factory=list)
    excluded_keywords: list = field(default_factory=list)
    write_excluded: bool = False
    autoreply_subject_prefixes: list = field(default_factory=list)
    autoreply_sender_patterns: list = field(default_factory=list)
    connection_roles: list = field(default_factory=list)
    # Dropped on the SUBJECT LINE ONLY (excluded_keywords also reads the body
    # preview, which over-matches on signatures and quoted threads). Used to
    # keep the HighPost / HIPstr funds out of the desk by topic, whoever writes.
    excluded_subject_keywords: list = field(default_factory=list)

    @classmethod
    def load(cls, path: Path):
        raw = json.loads(path.read_text())
        # Any "_"-prefixed key is an operator annotation, not a rule — JSON has
        # no comments and this file is meant to be edited by hand.
        raw = {k: v for k, v in raw.items() if not k.startswith("_")}
        return cls(**raw)


class Choices:
    """Choice option values. Each local option set numbers from the publisher's
    option-value base in declaration order (see solution/provision.py)."""

    def __init__(self, base: int):
        self.channel = {"Email": base, "Teams": base + 1}
        self.direction = {"Inbound": base, "Outbound": base + 1, "Internal": base + 2}
        self.rfistatus = {"NA": base, "Open": base + 1, "Answered": base + 2,
                          "Overdue": base + 3}  # Overdue reserved — never written
        self.matchmethod = {"Explicit": base, "Thread": base + 1, "ContactMatch": base + 2,
                            "Content": base + 3, "Manual": base + 4,
                            "Regarding": base + 5}  # appended in v2 (append-only!)
        self.matchstatus = {"Confirmed": base, "Suggested": base + 1,
                            "Unmatched": base + 2, "Excluded": base + 3}
        self.req_status = {"New": base, "InProgress": base + 1, "WaitingInternal": base + 2,
                           "WaitingExternal": base + 3, "Completed": base + 4,
                           "Cancelled": base + 5}
        # APPEND-ONLY — the last four were added 2026-08-04 (ir@ taxonomy,
        # docs/ir-triage-categories.md); order mirrors the option-set values.
        self.req_category = {n: base + i for i, n in enumerate(
            ["Reporting", "CapitalAccount", "Valuation", "KYC-AML", "SubscriptionDocs",
             "Legal-SideLetter", "Meeting", "DataRoom", "Other",
             "CapitalCall", "TaxDocs", "AccountAdmin", "LiquidityTransfer",
             "NDA",                                  # v3 2026-08-06 (append-only)
             "CartaOnboarding", "BrokerageDetails"])}  # v4 2026-08-09 (append-only)
        self.urgency = {"None": base, "UrgentLanguage": base + 1,
                        "ExplicitDeadline": base + 2}   # v2 WS6 (new_statedurgency)
        # close-readiness §2.2 routing (new_routingcategory, PROD 2026-08-03+)
        self.routing = {"ProcessBlocker": base, "Conviction": base + 1,
                        "DealMechanics": base + 2, "Scheduling": base + 3}
        # §4.4 draft tiers (new_drafttier); same T1-T4 order as the reference-table
        # tiers, so a routing rule's tier maps straight through.
        self.drafttier = {"T1": base, "T2": base + 1, "T3": base + 2, "T4": base + 3}
        # §4.5 inferred status (new_statusinferred) — system-set from thread traffic.
        self.statusinferred = {"AwaitingInvestor": base, "AwaitingInternal": base + 1,
                               "PossiblyClosable": base + 2}
        self.rev_direction = {v: k for k, v in self.direction.items()}
        self.rev_matchmethod = {v: k for k, v in self.matchmethod.items()}


@dataclass
class Config:
    tenant_id: str
    client_id: str
    client_secret: str
    dataverse_url: str
    prefix: str
    org_domains: list
    mailboxes: list
    choices: Choices
    ingest_floor: datetime
    fund_lookup: str  # opportunity→fund lookup logical name (Exigent: mint_fundorspv)
    rfi_due_bdays: int
    rfi_reply_status: str
    rules: Rules
    # v2 WS1 disposition thresholds (spec 1.3): >=auto_confirm_min → Confirmed;
    # review_min..auto_confirm_min-1 → needs_review; <review_min → noise
    auto_confirm_min: int = 85
    review_min: int = 50
    # §4.5: days of no traffic after our last outbound before a request is flagged
    # PossiblyClosable (never auto-closed — a human closes). Tunable.
    status_closable_days: int = 14
    # ir@ intake (docs/ir-intake-design.md): mailboxes whose inbound mail is
    # ingested even from unknown senders (auto-created contacts). Empty = off.
    intake_mailboxes: list = field(default_factory=list)
    # intake time floor: messages older than this never auto-create contacts
    # (bounds a delta-reset re-walk to the agreed window; None = ingest floor)
    intake_floor: datetime | None = None
    # Option-B split: only the restricted IR-request route creates Information
    # Requests. The broad engagement sync sets this False (signals only).
    create_requests: bool = True
    # Scope every active Dynamics contact, not just opportunity-linked ones.
    all_contacts: bool = False
    # Attach a follow-up to the open ticket on its thread instead of opening a
    # second one — gated on the classifier agreeing it is the same ask.
    merge_requests: bool = True
    merge_min_confidence: int = 70
    # v3 (2026-08-06): tag the engagement signal itself with its LLM category
    # (not just the request). Set True only on the restricted ir@ route; the
    # write is additionally gated by the new_category column existing in the
    # target env, so it no-ops until the v3 solution import lands.
    tag_signal_category: bool = False
    # (2026-08-08): don't open a request when the sender is a third party (CPA /
    # advisor / bank / custodian / law firm acting for — or instead of — an
    # investor). The signal is still logged + category-tagged; only the ticket
    # is suppressed, keeping the request feed to genuine investor inquiries.
    suppress_third_party_requests: bool = False

    @classmethod
    def from_env(cls, env: dict | None = None):
        env = env or load_env()
        url = env["DATAVERSE_URL"].rstrip("/")
        if "exigentcrmprod" in url.lower() and env.get("DATAVERSE_ALLOW_PROD") != "1":
            raise SystemExit(
                "REFUSING: DATAVERSE_URL points at PROD without DATAVERSE_ALLOW_PROD=1 "
                "(house rule 1 — set the flag only on explicit operator commit; "
                "schema still only ever arrives via manual solution import).")
        floor = env.get("INGEST_FLOOR", "2026-01-01")
        return cls(
            tenant_id=env["TENANT_ID"],
            client_id=env["CLIENT_ID"],
            client_secret=env["CLIENT_SECRET"],
            dataverse_url=url,
            prefix=env.get("PREFIX", "new_").rstrip("_") + "_",
            org_domains=[d.strip().lower() for d in env["ORG_DOMAINS"].split(",") if d.strip()],
            mailboxes=[m.strip().lower() for m in env["MAILBOXES"].split(",") if m.strip()],
            choices=Choices(int(env.get("CHOICE_VALUE_BASE", "100000000"))),
            ingest_floor=datetime.fromisoformat(floor).replace(tzinfo=timezone.utc),
            fund_lookup=env.get("FUND_LOOKUP", "mint_fundorspv"),
            auto_confirm_min=int(env.get("AUTO_CONFIRM_MIN", "85")),
            review_min=int(env.get("REVIEW_MIN", "50")),
            status_closable_days=int(env.get("STATUS_CLOSABLE_DAYS", "14")),
            rfi_due_bdays=int(env.get("RFI_DUE_BDAYS", "2")),
            rfi_reply_status=env.get("RFI_REPLY_STATUS", "WaitingExternal"),
            intake_mailboxes=[m.strip().lower() for m in
                              env.get("INTAKE_MAILBOXES", "").split(",") if m.strip()],
            intake_floor=(datetime.fromisoformat(env["INTAKE_FLOOR"])
                          .replace(tzinfo=timezone.utc)
                          if env.get("INTAKE_FLOOR") else None),
            create_requests=env.get("CREATE_REQUESTS", "1") != "0",
            all_contacts=env.get("ALL_CONTACTS", "0") == "1",
            merge_requests=env.get("MERGE_REQUESTS", "1") != "0",
            merge_min_confidence=int(env.get("MERGE_MIN_CONFIDENCE", "70")),
            tag_signal_category=env.get("TAG_SIGNAL_CATEGORY", "0") == "1",
            suppress_third_party_requests=env.get("SUPPRESS_THIRD_PARTY", "0") == "1",
            rules=Rules.load(Path(env.get("RULES_PATH", Path(__file__).parent / "rules.json"))),
        )
