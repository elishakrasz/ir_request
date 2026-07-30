"""WS6.2 — deterministic request escalation, computed at render/rollup time.

- amber: open > ESCALATE_AMBER business days (Sun–Thu, Asia/Jerusalem)
- red:   open > ESCALATE_RED business days, or past an explicit deadline
Thresholds config-driven (env ESCALATE_AMBER_BDAYS / ESCALATE_RED_BDAYS).
Never re-grades the LLM's stated urgency — that's creation-time only.
"""
import os
from datetime import datetime, timezone

from .bizhours import business_minutes

AMBER_BDAYS = int(os.environ.get("ESCALATE_AMBER_BDAYS", "2"))
RED_BDAYS = int(os.environ.get("ESCALATE_RED_BDAYS", "5"))
BIZ_DAY_MIN = 24 * 60   # a business "day" = one Sun–Thu calendar day


def escalation(created_utc: datetime, deadline_utc: datetime | None,
               now_utc: datetime | None = None) -> str:
    """'fresh' | 'amber' | 'red' for an OPEN request."""
    now = now_utc or datetime.now(timezone.utc)
    if deadline_utc is not None and now > deadline_utc:
        return "red"
    age = business_minutes(created_utc, now)
    if age > RED_BDAYS * BIZ_DAY_MIN:
        return "red"
    if age > AMBER_BDAYS * BIZ_DAY_MIN:
        return "amber"
    return "fresh"
