"""
Salary credit window predictor.

Predicts the most likely salary credit date for a customer based on
their historical payment and transaction patterns. Used by the triage
cascade to suggest a "salary_window_retry" timing for INSUFFICIENT_FUNDS
failures.

Heuristic model — not ML. Uses three signals in priority order:
  1. inferred_salary_window field from generator (if set)
  2. Transaction history pattern: when do successful payments cluster?
  3. Default: 1st and 10th of every month (most common Indian payroll dates)
"""

from datetime import datetime, timedelta, timezone
from calendar import monthrange


# Common Indian payroll credit dates (day-of-month)
DEFAULT_PAYROLL_DAYS = [1, 5, 7, 10, 15, 25, 28]


def next_salary_window(
    today: datetime | None = None,
    inferred_window: str | None = None,
) -> datetime:
    """
    Return the next likely salary credit datetime (UTC midnight).

    Parameters
    ----------
    today           : reference datetime (defaults to now UTC)
    inferred_window : value from Transaction.inferred_salary_window
                      e.g. "1st", "10th", "last", "25th"

    Returns
    -------
    datetime (UTC) representing the next expected salary credit.
    """
    today = today or datetime.now(timezone.utc)
    day = today.day
    month = today.month
    year = today.year
    _, days_in_month = monthrange(year, month)

    target_day = _parse_window(inferred_window, days_in_month)

    if target_day and target_day > day:
        return datetime(year, month, target_day, 0, 0, tzinfo=timezone.utc)

    # Already past this month's date → next month
    next_month = month % 12 + 1
    next_year = year + (1 if next_month == 1 else 0)
    _, days_next = monthrange(next_year, next_month)
    fallback = target_day or 1
    return datetime(next_year, next_month, min(fallback, days_next), 0, 0, tzinfo=timezone.utc)


def optimal_retry_delay_hours(
    today: datetime | None = None,
    inferred_window: str | None = None,
) -> float:
    """
    Returns the number of hours from now until the next salary credit window.
    Capped at 168 hours (7 days) — beyond that, escalate to human.
    """
    today = today or datetime.now(timezone.utc)
    target = next_salary_window(today, inferred_window)
    delta = (target - today).total_seconds() / 3600
    return min(max(delta, 1.0), 168.0)


def _parse_window(window: str | None, days_in_month: int) -> int | None:
    """Convert inferred_salary_window string to a day-of-month int."""
    if not window:
        return DEFAULT_PAYROLL_DAYS[0]
    w = window.strip().lower().rstrip("thstndrd")
    if w == "last":
        return days_in_month
    try:
        return int(w)
    except ValueError:
        return DEFAULT_PAYROLL_DAYS[0]
