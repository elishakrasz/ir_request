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
    excluded_senders: list = field(default_factory=list)
    excluded_domains: list = field(default_factory=list)
    excluded_keywords: list = field(default_factory=list)
    write_excluded: bool = False
    autoreply_subject_prefixes: list = field(default_factory=list)
    autoreply_sender_patterns: list = field(default_factory=list)
    connection_roles: list = field(default_factory=list)

    @classmethod
    def load(cls, path: Path):
        raw = json.loads(path.read_text())
        raw.pop("_comment", None)
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
                            "Content": base + 3, "Manual": base + 4}
        self.matchstatus = {"Confirmed": base, "Suggested": base + 1,
                            "Unmatched": base + 2, "Excluded": base + 3}
        self.req_status = {"New": base, "InProgress": base + 1, "WaitingInternal": base + 2,
                           "WaitingExternal": base + 3, "Completed": base + 4,
                           "Cancelled": base + 5}
        self.req_category = {n: base + i for i, n in enumerate(
            ["Reporting", "CapitalAccount", "Valuation", "KYC-AML", "SubscriptionDocs",
             "Legal-SideLetter", "Meeting", "DataRoom", "Other"])}
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
            rfi_due_bdays=int(env.get("RFI_DUE_BDAYS", "2")),
            rfi_reply_status=env.get("RFI_REPLY_STATUS", "WaitingExternal"),
            rules=Rules.load(Path(env.get("RULES_PATH", Path(__file__).parent / "rules.json"))),
        )
