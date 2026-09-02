"""
UPI error code → intervention mapping.

Maps Razorpay / NPCI UPI error codes to structured intervention hints
used by the triage cascade's rule-based Tier-1 classifier.

Reference: NPCI UPI error codes v2.0 (public specification)
"""

UPI_CODE_MAP: dict[str, dict] = {
    # ── Insufficient funds ────────────────────────────────────────────────
    "U16":  {"failure_class": "insufficient_funds",  "recommended_action": "salary_window_retry",  "recoverable": True,  "description": "Debtor account does not have sufficient funds"},
    "B16":  {"failure_class": "insufficient_funds",  "recommended_action": "salary_window_retry",  "recoverable": True,  "description": "Insufficient balance"},

    # ── VPA / account issues ──────────────────────────────────────────────
    "U30":  {"failure_class": "vpa_not_found",        "recommended_action": "update_vpa_flow",       "recoverable": True,  "description": "VPA not registered"},
    "U31":  {"failure_class": "vpa_not_found",        "recommended_action": "update_vpa_flow",       "recoverable": True,  "description": "VPA deactivated"},
    "RB":   {"failure_class": "vpa_not_found",        "recommended_action": "update_vpa_flow",       "recoverable": True,  "description": "Invalid VPA/beneficiary"},

    # ── Bank server / network ─────────────────────────────────────────────
    "U69":  {"failure_class": "bank_server_down",     "recommended_action": "wait_retry",            "recoverable": True,  "description": "Remitter bank offline"},
    "U70":  {"failure_class": "bank_server_down",     "recommended_action": "wait_retry",            "recoverable": True,  "description": "Beneficiary bank offline"},
    "U09":  {"failure_class": "bank_server_down",     "recommended_action": "wait_retry",            "recoverable": True,  "description": "Transaction timed out at bank"},
    "XT":   {"failure_class": "bank_server_down",     "recommended_action": "wait_retry",            "recoverable": True,  "description": "Technical timeout"},

    # ── Authentication failures ───────────────────────────────────────────
    "U14":  {"failure_class": "auth_failure",         "recommended_action": "payment_link",          "recoverable": True,  "description": "Wrong MPIN entered"},
    "U05":  {"failure_class": "auth_failure",         "recommended_action": "payment_link",          "recoverable": True,  "description": "Transaction not permitted"},
    "RZ":   {"failure_class": "auth_failure",         "recommended_action": "payment_link",          "recoverable": True,  "description": "Risk threshold exceeded at NPCI"},

    # ── Card expired ──────────────────────────────────────────────────────
    "CE":   {"failure_class": "card_expired",         "recommended_action": "payment_link",          "recoverable": True,  "description": "Card expired"},
    "54":   {"failure_class": "card_expired",         "recommended_action": "payment_link",          "recoverable": True,  "description": "Card expired (ISO 8583)"},

    # ── Mandate ───────────────────────────────────────────────────────────
    "ME":   {"failure_class": "mandate_expired",      "recommended_action": "reauth_mandate",        "recoverable": True,  "description": "Mandate expired"},
    "MR":   {"failure_class": "mandate_expired",      "recommended_action": "reauth_mandate",        "recoverable": True,  "description": "Mandate revoked by customer"},

    # ── Limit exceeded ────────────────────────────────────────────────────
    "U28":  {"failure_class": "limit_exceeded",       "recommended_action": "split_payment",         "recoverable": True,  "description": "Amount exceeds per-transaction limit"},
    "U29":  {"failure_class": "limit_exceeded",       "recommended_action": "split_payment",         "recoverable": True,  "description": "Amount exceeds daily limit"},
    "LE":   {"failure_class": "limit_exceeded",       "recommended_action": "split_payment",         "recoverable": True,  "description": "UPI limit exceeded"},

    # ── Checkout / abandon ────────────────────────────────────────────────
    "CA":   {"failure_class": "checkout_abandoned",   "recommended_action": "sms_reminder",          "recoverable": True,  "description": "Customer abandoned checkout"},
    "TD":   {"failure_class": "checkout_abandoned",   "recommended_action": "sms_reminder",          "recoverable": True,  "description": "Transaction declined by customer"},
}


def classify_upi_code(code: str) -> dict:
    """
    Look up a UPI error code and return the intervention hint dict.
    Returns a best-effort 'unknown' entry if the code isn't mapped.
    """
    result = UPI_CODE_MAP.get((code or "").upper())
    if result:
        return result
    return {
        "failure_class": "unknown",
        "recommended_action": "human_escalation",
        "recoverable": False,
        "description": f"Unmapped UPI code: {code}",
    }
