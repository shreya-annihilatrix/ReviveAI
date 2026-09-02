"""
India festival and bonus payout calendar.

Payment recovery rates vary significantly around major Indian festivals
and bonus seasons. This module provides a simple lookup so the strategy
agent can:

  1. Delay "salary_window_retry" actions until after bonus credit.
  2. Prefer payment links over retries during high-traffic festival periods
     (bank servers experience elevated load).
  3. Avoid retry attempts immediately before festivals (customers are
     busy; low engagement probability).

Data: hand-coded from RBI holiday calendar + common corporate bonus cycles.
A production system would pull this from a maintained external feed.
"""

from datetime import date, timedelta


# ---------------------------------------------------------------------------
# Festival / bonus date definitions (year-agnostic where possible)
# Format: (month, day) → (name, type, pre_days, post_days)
#   pre_days  = days BEFORE the date to avoid interventions
#   post_days = days AFTER the date that bonus typically credits
# ---------------------------------------------------------------------------

FESTIVAL_CALENDAR: dict[tuple[int, int], dict] = {
    # Diwali (approximate — moves each year with lunar calendar)
    (10, 20): {"name": "Diwali",         "type": "festival",     "pre_days": 3,  "post_days": 5},
    (11, 1):  {"name": "Diwali bonus",   "type": "bonus_credit", "pre_days": 0,  "post_days": 7},

    # Holi
    (3, 25):  {"name": "Holi",           "type": "festival",     "pre_days": 2,  "post_days": 2},

    # Eid (approximate)
    (4, 10):  {"name": "Eid",            "type": "festival",     "pre_days": 2,  "post_days": 3},

    # Republic Day
    (1, 26):  {"name": "Republic Day",   "type": "holiday",      "pre_days": 0,  "post_days": 0},

    # Independence Day
    (8, 15):  {"name": "Independence Day", "type": "holiday",    "pre_days": 0,  "post_days": 0},

    # Year-end / Jan salary bump
    (1, 5):   {"name": "Year-end bonus", "type": "bonus_credit", "pre_days": 0,  "post_days": 10},

    # Dussehra
    (10, 12): {"name": "Dussehra",       "type": "festival",     "pre_days": 1,  "post_days": 3},

    # Pongal / Makar Sankranti
    (1, 14):  {"name": "Pongal",         "type": "festival",     "pre_days": 1,  "post_days": 2},

    # Navratri start (approx)
    (10, 3):  {"name": "Navratri",       "type": "festival",     "pre_days": 0,  "post_days": 0},
}


def get_upcoming_events(reference: date | None = None, lookahead_days: int = 14) -> list[dict]:
    """
    Return a list of festival/bonus events within the next `lookahead_days` days.

    Each entry has: name, type, event_date, days_until, post_days
    """
    today = reference or date.today()
    events = []
    for (month, day), meta in FESTIVAL_CALENDAR.items():
        try:
            event_date = date(today.year, month, day)
        except ValueError:
            continue  # invalid date (e.g. Feb 29 in non-leap year)
        days_until = (event_date - today).days
        if 0 <= days_until <= lookahead_days:
            events.append({
                "name":       meta["name"],
                "type":       meta["type"],
                "event_date": event_date.isoformat(),
                "days_until": days_until,
                "post_days":  meta["post_days"],
                "pre_days":   meta["pre_days"],
            })
    return sorted(events, key=lambda e: e["days_until"])


def is_bonus_season(reference: date | None = None, window_days: int = 7) -> bool:
    """
    Return True if a bonus_credit event is within the next `window_days`.
    Use this to prefer delayed retry actions — customers will have more funds.
    """
    events = get_upcoming_events(reference, lookahead_days=window_days)
    return any(e["type"] == "bonus_credit" for e in events)


def should_avoid_intervention(reference: date | None = None) -> bool:
    """
    Return True if today is within the pre-festival window of any festival,
    meaning the customer is likely distracted and engagement rates will be low.
    """
    today = reference or date.today()
    for (month, day), meta in FESTIVAL_CALENDAR.items():
        try:
            event_date = date(today.year, month, day)
        except ValueError:
            continue
        days_until = (event_date - today).days
        if 0 <= days_until <= meta["pre_days"]:
            return True
    return False
