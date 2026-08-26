"""
Tier 1 — Deterministic rule lookup for known Razorpay failure codes.

Match priority (first match wins):
  1. (failure_code, payment_method, bank)   — exact
  2. (failure_code, payment_method, "*")    — any bank
  3. (failure_code, "*",            "*")    — any method + bank

All keys are normalised to UPPER CASE before lookup.
A result with confidence >= RULES_CONFIDENCE_THRESHOLD is accepted
by the cascade without calling the LLM.
"""

from typing import Literal, TypedDict

# Minimum confidence to accept a rule match without LLM escalation
RULES_CONFIDENCE_THRESHOLD = 0.80

FailureType = Literal[
    "insufficient_funds",
    "bank_degradation",
    "vpa_invalid",
    "auth_failure",
    "limit_exceeded",
    "expired_instrument",
    "mandate_failure",
    "unknown",
]

class RuleResult(TypedDict):
    failure_type: str
    root_cause: str
    confidence: float
    recommended_channel: str


# ---------------------------------------------------------------------------
# Rule table
# Key: (failure_code_upper, payment_method_upper_or_STAR, bank_upper_or_STAR)
# ---------------------------------------------------------------------------

RULES: dict[tuple[str, str, str], RuleResult] = {

    # ── Razorpay UPI native error codes ─────────────────────────────────────
    ("U16", "UPI", "*"): {
        "failure_type": "limit_exceeded",
        "root_cause": "upi_transaction_limit",
        "confidence": 0.97,
        "recommended_channel": "split_payment",
    },
    ("U30", "UPI", "*"): {
        "failure_type": "vpa_invalid",
        "root_cause": "vpa_not_found",
        "confidence": 0.99,
        "recommended_channel": "payment_link",
    },
    ("U69", "UPI", "*"): {
        "failure_type": "bank_degradation",
        "root_cause": "server_down",
        "confidence": 0.95,
        "recommended_channel": "wait_and_retry",
    },
    ("BR", "UPI", "*"): {
        "failure_type": "insufficient_funds",
        "root_cause": "low_balance",
        "confidence": 0.93,
        "recommended_channel": "salary_window_retry",
    },

    # ── Synthetic / normalised failure codes — UPI ───────────────────────────
    ("VPA_NOT_FOUND", "UPI", "*"): {
        "failure_type": "vpa_invalid",
        "root_cause": "vpa_not_found",
        "confidence": 0.99,
        "recommended_channel": "update_vpa_flow",
    },
    ("VPA_DUPLICATE", "UPI", "*"): {
        "failure_type": "vpa_invalid",
        "root_cause": "duplicate_vpa_registration",
        "confidence": 0.97,
        "recommended_channel": "update_vpa_flow",
    },
    ("UPI_PIN_LOCKED", "UPI", "*"): {
        "failure_type": "auth_failure",
        "root_cause": "upi_pin_locked_3_attempts",
        "confidence": 0.97,
        "recommended_channel": "payment_link",
    },
    ("LIMIT_EXCEEDED", "UPI", "*"): {
        "failure_type": "limit_exceeded",
        "root_cause": "upi_daily_limit",
        "confidence": 0.96,
        "recommended_channel": "split_payment",
    },
    ("BANK_SERVER_DOWN", "UPI", "*"): {
        "failure_type": "bank_degradation",
        "root_cause": "upi_bank_server_down",
        "confidence": 0.96,
        "recommended_channel": "wait_and_retry",
    },
    ("INSUFFICIENT_FUNDS", "UPI", "*"): {
        "failure_type": "insufficient_funds",
        "root_cause": "low_upi_balance",
        "confidence": 0.94,
        "recommended_channel": "salary_window_retry",
    },

    # ── Card failures ────────────────────────────────────────────────────────
    ("CARD_EXPIRED", "CARD", "*"): {
        "failure_type": "expired_instrument",
        "root_cause": "card_past_expiry",
        "confidence": 0.99,
        "recommended_channel": "payment_method_update",
    },
    ("AUTH_FAILURE", "CARD", "*"): {
        "failure_type": "auth_failure",
        "root_cause": "card_authentication_failed",
        "confidence": 0.92,
        "recommended_channel": "payment_link",
    },
    ("INTL_CARD_BLOCKED", "CARD", "*"): {
        "failure_type": "auth_failure",
        "root_cause": "international_card_on_domestic_rails",
        "confidence": 0.98,
        "recommended_channel": "payment_method_update",
    },
    ("CARD_NETWORK_TIMEOUT", "CARD", "*"): {
        "failure_type": "bank_degradation",
        "root_cause": "card_network_timeout",
        "confidence": 0.93,
        "recommended_channel": "wait_and_retry",
    },
    ("LIMIT_EXCEEDED", "CARD", "*"): {
        "failure_type": "limit_exceeded",
        "root_cause": "card_credit_limit",
        "confidence": 0.94,
        "recommended_channel": "payment_link",
    },
    ("INSUFFICIENT_FUNDS", "CARD", "*"): {
        "failure_type": "insufficient_funds",
        "root_cause": "insufficient_card_credit",
        "confidence": 0.91,
        "recommended_channel": "payment_link",
    },

    # ── Netbanking ───────────────────────────────────────────────────────────
    ("AUTH_FAILURE", "NETBANKING", "*"): {
        "failure_type": "auth_failure",
        "root_cause": "netbanking_auth_failed",
        "confidence": 0.91,
        "recommended_channel": "payment_link",
    },
    ("BANK_SERVER_DOWN", "NETBANKING", "*"): {
        "failure_type": "bank_degradation",
        "root_cause": "netbanking_server_down",
        "confidence": 0.95,
        "recommended_channel": "wait_and_retry",
    },
    ("INSUFFICIENT_FUNDS", "NETBANKING", "*"): {
        "failure_type": "insufficient_funds",
        "root_cause": "low_bank_balance",
        "confidence": 0.91,
        "recommended_channel": "salary_window_retry",
    },
    ("BENEFICIARY_UNREACHABLE", "NETBANKING", "*"): {
        "failure_type": "bank_degradation",
        "root_cause": "beneficiary_bank_unreachable",
        "confidence": 0.94,
        "recommended_channel": "wait_and_retry",
    },

    # ── Mandate / emandate ───────────────────────────────────────────────────
    ("MANDATE_EXPIRED", "EMANDATE", "*"): {
        "failure_type": "mandate_failure",
        "root_cause": "emandate_expired",
        "confidence": 0.98,
        "recommended_channel": "reauth_flow",
    },
    ("LIMIT_EXCEEDED", "EMANDATE", "*"): {
        "failure_type": "mandate_failure",
        "root_cause": "mandate_debit_limit_exceeded",
        "confidence": 0.93,
        "recommended_channel": "reauth_flow",
    },

    # ── Wallet ───────────────────────────────────────────────────────────────
    ("INSUFFICIENT_FUNDS", "WALLET", "*"): {
        "failure_type": "insufficient_funds",
        "root_cause": "low_wallet_balance",
        "confidence": 0.90,
        "recommended_channel": "payment_link",
    },
    ("LIMIT_EXCEEDED", "WALLET", "*"): {
        "failure_type": "limit_exceeded",
        "root_cause": "wallet_transaction_limit",
        "confidence": 0.92,
        "recommended_channel": "payment_link",
    },

    # ── Cross-method catch-alls ──────────────────────────────────────────────
    ("INSUFFICIENT_FUNDS", "*", "*"): {
        "failure_type": "insufficient_funds",
        "root_cause": "low_balance",
        "confidence": 0.88,
        "recommended_channel": "salary_window_retry",
    },
    ("BANK_SERVER_DOWN", "*", "*"): {
        "failure_type": "bank_degradation",
        "root_cause": "server_down",
        "confidence": 0.90,
        "recommended_channel": "wait_and_retry",
    },
    ("AUTH_FAILURE", "*", "*"): {
        "failure_type": "auth_failure",
        "root_cause": "authentication_failed",
        "confidence": 0.85,
        "recommended_channel": "payment_link",
    },
    ("MANDATE_EXPIRED", "*", "*"): {
        "failure_type": "mandate_failure",
        "root_cause": "mandate_expired",
        "confidence": 0.95,
        "recommended_channel": "reauth_flow",
    },
    ("LIMIT_EXCEEDED", "*", "*"): {
        "failure_type": "limit_exceeded",
        "root_cause": "payment_limit_exceeded",
        "confidence": 0.88,
        "recommended_channel": "split_payment",
    },
    ("VELOCITY_CHECK_FAILED", "*", "*"): {
        "failure_type": "limit_exceeded",
        "root_cause": "velocity_check_exceeded",
        "confidence": 0.95,
        "recommended_channel": "payment_link",
    },
    ("CHECKOUT_ABANDONED", "*", "*"): {
        "failure_type": "unknown",
        "root_cause": "customer_abandoned_checkout",
        "confidence": 0.85,
        "recommended_channel": "payment_link",
    },
    ("BENEFICIARY_UNREACHABLE", "*", "*"): {
        "failure_type": "bank_degradation",
        "root_cause": "beneficiary_bank_unreachable",
        "confidence": 0.92,
        "recommended_channel": "wait_and_retry",
    },
}


# ---------------------------------------------------------------------------
# Lookup function
# ---------------------------------------------------------------------------

def lookup(
    failure_code: str,
    payment_method: str,
    bank: str = "*",
) -> RuleResult | None:
    """
    Return the best matching rule or None if no rule covers this combination.

    Match priority:
      1. exact  (code, method, bank)
      2. method (code, method, *)
      3. catch  (code, *, *)
    """
    fc  = (failure_code   or "").upper().strip()
    pm  = (payment_method or "").upper().strip()
    bk  = (bank           or "").upper().strip()

    return (
        RULES.get((fc, pm, bk))
        or RULES.get((fc, pm, "*"))
        or RULES.get((fc, "*",  "*"))
    )
