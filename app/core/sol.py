"""SOL day counter: days elapsed since the mission landing date."""
from __future__ import annotations

from datetime import date, datetime


def parse_landing(landing_iso: str) -> date | None:
    if not landing_iso:
        return None
    try:
        return date.fromisoformat(str(landing_iso)[:10])
    except ValueError:
        return None


def sol_days(landing_iso: str, when: datetime | None = None) -> int:
    """SOL = whole days since landing date (0 on landing day)."""
    landing = parse_landing(landing_iso)
    if landing is None:
        return 0
    when = when or datetime.now()
    return max(0, (when.date() - landing).days)
