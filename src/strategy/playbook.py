"""
Rule Playbook — deterministic strategy for all tier-1 triage outputs.

Maps failure_type → (primary Action, fallback Action).
Used by the strategy agent BEFORE considering LLM proposals.
Covers ~85% of transactions with zero LLM cost.
"""

from dataclasses import dataclass
from typing import Literal


# ---------------------------------------------------------------------------
# Action dataclass
# ---------------------------------------------------------------------------

ActionType = Literal[
    "salary_window_retry",
    "payment_link",
    "wait_retry",
    "update_vpa_flow",
    "payment_method_update",
    "reauth_flow",
    "split_payment",
    "sms_reminder",
    "whatsapp_reminder",
    "human_escalation",
    "do_nothing",
]

Channel = Literal[
    "payment_link",
    "retry_same",
    "sms",
    "whatsapp",
    "email",
    "human",
    "none",
]


@dataclass(frozen=True)
class Action:
    type: str        # ActionType
    timing: str      # e.g. "immediate", "+2h", "after_salary_credit+1d"
    channel: str     # Channel
    priority: int = 1   # 1=primary, 2=fallback


# ---------------------------------------------------------------------------
# Playbook
# ---------------------------------------------------------------------------

PLAYBOOK: dict[str, dict[str, Action]] = {

    "insufficient_funds": {
        "primary": Action(
            type="salary_window_retry",
            timing="after_salary_credit+2d",
            channel="payment_link",
            priority=1,
        ),
        "fallback": Action(
            type="sms_reminder",
            timing="after_salary_credit+1d",
            channel="sms",
            priority=2,
        ),
    },

    "vpa_invalid": {
        "primary": Action(
            type="payment_link",
            timing="immediate",
            channel="payment_link",
            priority=1,
        ),
        "fallback": Action(
            type="sms_reminder",
            timing="+1h",
            channel="sms",
            priority=2,
        ),
    },

    "bank_degradation": {
        "primary": Action(
            type="wait_retry",
            timing="+2h",
            channel="retry_same",
            priority=1,
        ),
        "fallback": Action(
            type="payment_link",
            timing="+3h",
            channel="payment_link",
            priority=2,
        ),
    },

    "auth_failure": {
        "primary": Action(
            type="payment_link",
            timing="+30m",
            channel="payment_link",
            priority=1,
        ),
        "fallback": Action(
            type="whatsapp_reminder",
            timing="+2h",
            channel="whatsapp",
            priority=2,
        ),
    },

    "limit_exceeded": {
        "primary": Action(
            type="split_payment",
            timing="immediate",
            channel="payment_link",
            priority=1,
        ),
        "fallback": Action(
            type="payment_link",
            timing="+1h",
            channel="payment_link",
            priority=2,
        ),
    },

    "expired_instrument": {
        "primary": Action(
            type="payment_method_update",
            timing="immediate",
            channel="payment_link",
            priority=1,
        ),
        "fallback": Action(
            type="whatsapp_reminder",
            timing="+30m",
            channel="whatsapp",
            priority=2,
        ),
    },

    "mandate_failure": {
        "primary": Action(
            type="reauth_flow",
            timing="immediate",
            channel="payment_link",
            priority=1,
        ),
        "fallback": Action(
            type="human_escalation",
            timing="+4h",
            channel="human",
            priority=2,
        ),
    },

    "unknown": {
        "primary": Action(
            type="payment_link",
            timing="+1h",
            channel="payment_link",
            priority=1,
        ),
        "fallback": Action(
            type="do_nothing",
            timing="none",
            channel="none",
            priority=2,
        ),
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_actions(failure_type: str) -> dict[str, Action]:
    """
    Return the playbook entry for a failure type.
    Falls back to 'unknown' if the type is not in the playbook.
    """
    return PLAYBOOK.get(failure_type, PLAYBOOK["unknown"])


def get_primary(failure_type: str) -> Action:
    return get_actions(failure_type)["primary"]


def get_fallback(failure_type: str) -> Action:
    return get_actions(failure_type)["fallback"]


def all_actions_for(failure_type: str) -> list[Action]:
    """Return [primary, fallback] as a list."""
    entry = get_actions(failure_type)
    return [entry["primary"], entry["fallback"]]
