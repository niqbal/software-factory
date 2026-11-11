"""Recognise model usage-limit errors and when they lift.

Claude Code reports e.g. "You've hit your 5-hour limit · resets 1pm (Asia/Karachi)". Every
project shares one subscription, so a limit stops all model work until the reset.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LIMIT_PATTERNS = [re.compile(p, re.I) for p in (
    r"hit your [\w\s-]*limit", r"usage limit", r"rate limit", r"limit reached", r"\b429\b",
    r"overloaded",
)]
RESET = re.compile(r"resets\s+(?:(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),?\s+)?(?:at\s+)?"
                   r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?(?:\s*\((?P<tz>[^)]+)\))?", re.I)
FALLBACK = timedelta(minutes=60)


def is_limit_error(text: str | None) -> bool:
    return bool(text) and any(p.search(text) for p in LIMIT_PATTERNS)


def reset_time(text: str, now: datetime | None = None) -> datetime:
    """When the limit lifts, as an aware datetime. Falls back to an hour from now."""
    now = now or datetime.now().astimezone()
    m = RESET.search(text or "")
    if not m:
        return now + FALLBACK
    tz = now.tzinfo
    if m.group("tz"):
        try:
            tz = ZoneInfo(m.group("tz").strip())
        except (ZoneInfoNotFoundError, ValueError):
            pass
    local_now = now.astimezone(tz)
    hour = int(m.group("h")) % 12 if m.group("ap") else int(m.group("h"))
    if (m.group("ap") or "").lower() == "pm":
        hour += 12
    minute = int(m.group("m") or 0)
    if m.group("mon"):
        month = datetime.strptime(m.group("mon"), "%b").month
        when = local_now.replace(month=month, day=int(m.group("day")), hour=hour, minute=minute, second=0,
                                 microsecond=0)
        if when < local_now - timedelta(days=1):
            when = when.replace(year=when.year + 1)
    else:
        when = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= local_now:
            when += timedelta(days=1)
    return when + timedelta(minutes=1)  # a minute of slack past the stated reset
