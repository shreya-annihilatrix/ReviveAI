"""
Compliance Gate - Phase 7.

Ensures actions respect quiet hours, frequency caps, opt-outs, and DND.
"""

import logging
from datetime import datetime, timedelta
from src.strategy.llm_strategy import ActionProposal
from src.gates.models import GateResult
from src.gates.security import mask_pii

log = logging.getLogger(__name__)

QUIET_HOURS_IST = (21, 9)   # 21:00 to 09:00 IST - TRAI commercial norms
FREQUENCY_CAP = 3           # max contacts per customer per 7 days
CHANNELS = ["sms", "whatsapp", "call"]

def in_quiet_hours(dt_utc: datetime) -> bool:
    if not dt_utc: return False
    ist_time = dt_utc + timedelta(hours=5, minutes=30)
    hour = ist_time.hour
    if hour >= QUIET_HOURS_IST[0] or hour < QUIET_HOURS_IST[1]:
        return True
    return False

def sanitize_for_prompt(text: str) -> str:
    """Wraps customer data to prevent prompt injection."""
    if not text:
        return ""
    # Strip any existing tags to prevent escaping
    text = text.replace("<customer_data>", "").replace("</customer_data>", "")
    return f"<customer_data>\n{text}\n</customer_data>"

class ComplianceGate:
    def validate(self, proposal, customer, contact_history, txn=None) -> GateResult:
        action_type = proposal.get("action_type") if isinstance(proposal, dict) else proposal.action_type
        if action_type == "do_nothing":
            return GateResult(approved=True)

        recent_contacts = len(contact_history)
        
        channel = proposal.get("channel") if isinstance(proposal, dict) else getattr(proposal, "channel", None)
        schedule_at = proposal.get("schedule_at") if isinstance(proposal, dict) else getattr(proposal, "schedule_at", None)
        
        opted_out = customer.get("opted_out", False) if isinstance(customer, dict) else getattr(customer, "opted_out", False)
        
        # Mandate check
        mandate_expired = False
        if txn:
            payment_method = txn.get("payment_method") if isinstance(txn, dict) else getattr(txn, "payment_method", None)
            mandate_expiry = txn.get("mandate_expiry") if isinstance(txn, dict) else getattr(txn, "mandate_expiry", None)
            failure_code = txn.get("failure_code") if isinstance(txn, dict) else getattr(txn, "failure_code", None)
            
            if payment_method == "emandate" or failure_code == "MANDATE_EXPIRED":
                # If they try to retry an expired mandate, block it
                if action_type == "retry_same":
                    mandate_expired = True

        # Order matters: absolute rules (opt-out, mandate) first,
        # then timing rules (quiet hours), then rate limits.
        # This ensures the most specific/permanent reason is always reported.
        checks = [
            (not opted_out,                                                     "customer_opted_out"),
            (not mandate_expired,                                                "mandate_expired"),
            (not (channel in CHANNELS and in_quiet_hours(schedule_at)),         "quiet_hours_violation"),
            (recent_contacts < FREQUENCY_CAP,                                   "frequency_cap_exceeded"),
        ]
        
        for passes, reason in checks:
            if not passes:
                return GateResult(approved=False, reason=reason)
                
        return GateResult(approved=True)

# Backwards compatibility
def validate(proposal: ActionProposal, customer, contact_history: list, txn=None) -> GateResult:
    return ComplianceGate().validate(proposal, customer, contact_history, txn)
