"""
Compliance Gate — Phase 7.

Ensures actions respect quiet hours, frequency caps, opt-outs, and DND.
"""

import logging
from datetime import datetime, timedelta
from src.strategy.llm_strategy import ActionProposal
from src.gates.models import GateResult
from src.gates.security import mask_pii

log = logging.getLogger(__name__)

QUIET_HOURS_IST = (21, 9)   # 21:00 to 09:00 IST — TRAI commercial norms
FREQUENCY_CAP = 3           # max contacts per customer per 7 days
CHANNELS = ["sms", "whatsapp", "call"]

def in_quiet_hours(dt_utc: datetime) -> bool:
    # Convert UTC to IST (+5:30)
    ist_time = dt_utc + timedelta(hours=5, minutes=30)
    hour = ist_time.hour
    
    # if hour >= 21 OR hour < 9
    if hour >= QUIET_HOURS_IST[0] or hour < QUIET_HOURS_IST[1]:
        return True
    return False

def contact_count_7d(customer_id: int) -> int:
    # Mock for checkpoint/demo purposes
    return 0

def on_dnd_registry(phone: str) -> bool:
    # Mock for checkpoint/demo purposes
    return False

def mandate_valid_if_required(proposal: ActionProposal, customer) -> bool:
    # Mock for checkpoint/demo purposes
    return True

def log_compliance_rejection(customer, proposal: ActionProposal, reason: str, amount_forgone: float):
    # Masking customer phone / info if we log it
    masked_phone = mask_pii(getattr(customer, "phone", "9999999999"))
    log.warning(
        f"Compliance Rejection (cust={customer.id}, phone={masked_phone}): "
        f"{reason}. Rs.{amount_forgone:.2f} forgone."
    )

def validate(proposal: ActionProposal, customer, contact_history: list, txn) -> GateResult:
    if proposal.action_type == "do_nothing":
        return GateResult(approved=True)

    # Use history if provided, else use mock
    recent_contacts = len(contact_history) if contact_history else contact_count_7d(customer.id)

    checks = [
        (not (proposal.channel in CHANNELS and in_quiet_hours(proposal.schedule_at)), "quiet_hours_violation"),
        (recent_contacts < FREQUENCY_CAP, "frequency_cap_exceeded"),
        (not getattr(customer, "opted_out", False), "customer_opted_out"),
        (not on_dnd_registry(getattr(customer, "phone", "9999999999")), "dnd_registry"),
        (mandate_valid_if_required(proposal, customer), "mandate_expired"),
    ]
    
    for passes, reason in checks:
        if not passes:
            log_compliance_rejection(customer, proposal, reason, txn.amount)
            return GateResult(approved=False, reason=reason)
            
    return GateResult(approved=True)
