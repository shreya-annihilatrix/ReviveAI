"""
Bank degradation monitor.

Tracks real-time bank availability signals. Used by the triage cascade
to decide whether a BANK_SERVER_DOWN failure is likely transient (retry in
minutes) or systemic (retry in hours, or escalate).

In production this would poll Razorpay's bank status API. In this demo
it uses a configurable mock table that can be overridden via environment
variables (DEGRADED_BANKS=SBI,HDFC) for demo purposes.

Degradation levels:
  HEALTHY   → normal operations
  DEGRADED  → elevated error rate, retry in 30–60 min
  DOWN      → confirmed outage, retry in 4–8 h
"""

import os
from datetime import datetime, timezone

# Env override: comma-separated list of bank codes currently degraded/down
_DEGRADED_ENV = os.getenv("DEGRADED_BANKS", "").upper()
_DOWN_ENV = os.getenv("DOWN_BANKS", "").upper()

DEGRADED_BANKS: set[str] = {b.strip() for b in _DEGRADED_ENV.split(",") if b.strip()}
DOWN_BANKS: set[str] = {b.strip() for b in _DOWN_ENV.split(",") if b.strip()}

# Retry delay recommendations (hours)
RETRY_HOURS = {
    "HEALTHY":  0.5,    # immediate retry after short wait
    "DEGRADED": 1.0,    # wait 1 hour
    "DOWN":     6.0,    # wait 6 hours
}


def get_bank_status(bank_code: str) -> str:
    """
    Return the current degradation status for a bank.

    Parameters
    ----------
    bank_code : e.g. "SBI", "HDFC", "ICICI", "AXIS"

    Returns
    -------
    "HEALTHY" | "DEGRADED" | "DOWN"
    """
    code = (bank_code or "").upper().strip()
    if code in DOWN_BANKS:
        return "DOWN"
    if code in DEGRADED_BANKS:
        return "DEGRADED"
    return "HEALTHY"


def recommended_retry_hours(bank_code: str) -> float:
    """
    Return the recommended number of hours to wait before retrying
    a bank-related failure for the given bank.
    """
    status = get_bank_status(bank_code)
    return RETRY_HOURS[status]


def is_bank_healthy(bank_code: str) -> bool:
    """Convenience check — returns True if no degradation is detected."""
    return get_bank_status(bank_code) == "HEALTHY"


def status_summary() -> dict[str, str]:
    """
    Return a dict of all known non-healthy banks and their status.
    Used by the Streamlit dashboard for the bank health panel.
    """
    summary = {}
    for bank in DOWN_BANKS:
        summary[bank] = "DOWN"
    for bank in DEGRADED_BANKS - DOWN_BANKS:
        summary[bank] = "DEGRADED"
    return summary
