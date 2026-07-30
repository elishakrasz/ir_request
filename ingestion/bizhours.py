"""Business-time math (WS3): Asia/Jerusalem, Sunday–Thursday work week.

A "business minute" is any minute falling on a Sun–Thu calendar day in the
configured timezone (full days — no intra-day office hours; config can narrow
later if wanted). Friday and Saturday contribute zero.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jerusalem")
# Python weekday(): Mon=0 … Sun=6. Work week Sun–Thu.
WORKDAYS = {6, 0, 1, 2, 3}


def business_minutes(start_utc: datetime, end_utc: datetime,
                     tz: ZoneInfo = TZ, workdays: set = WORKDAYS) -> int:
    """Minutes between two aware datetimes that fall on workdays (local tz)."""
    if end_utc <= start_utc:
        return 0
    start = start_utc.astimezone(tz)
    end = end_utc.astimezone(tz)
    total = 0
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        nxt = (day + timedelta(days=1, hours=12)).replace(
            hour=0, minute=0, second=0, microsecond=0)  # DST-safe next midnight
        if day.weekday() in workdays:
            lo, hi = max(start, day), min(end, nxt)
            if hi > lo:
                total += int((hi - lo).total_seconds() // 60)
        day = nxt
    return total
