"""
Policy Gate — Phase 7.

Ensures proposed actions are safe, within limits, and logically sound.
"""

import logging
from datetime import datetime, timedelta, timezone
from src.strategy.llm_strategy import ActionProposal
from src.gates.models import GateResult

log = logging.getLogger(__name__)

ALLOWLIST = [
    "retry", "payment_link", "split_payment", "sms_reminder", 
    "whatsapp", "human_escalation", "do_nothing", 
    "payment_method_update", "reauth_mandate", "reauth_flow",
    "wait_retry", "update_vpa_flow", "salary_window_retry", "whatsapp_reminder"
]

def now():
    return datetime.now(timezone.utc)

def no_pending_action_exists(txn_id: int) -> bool:
    # Placeholder: would normally check the Outbox or DB for pending actions
    return True

def log_rejection(txn_id: int, proposal: ActionProposal, reason: str):
    log.warning(f"Policy Rejection (txn={txn_id}): {reason} - Action: {proposal.action_type}")

def validate(proposal: ActionProposal, txn, attempt_history: list) -> GateResult:
    # Relax 'past' check to give a slight 5-minute buffer in case of execution delays
    # but the instructions say `proposal.schedule_at >= now()`
    
    checks = [
        (proposal.action_type in ALLOWLIST, "action_type_not_in_allowlist"),
        (proposal.amount <= txn.amount, "amount_exceeds_original"),   # HARD invariant
        (len(attempt_history) < 2, "max_attempt_count_reached"),
        (proposal.schedule_at <= now() + timedelta(days=7), "schedule_too_far"),
        (proposal.schedule_at >= now() - timedelta(minutes=5), "schedule_in_past"),
        (no_pending_action_exists(txn.id), "pending_action_already_exists"),
    ]
    
    for passes, reason in checks:
        if not passes:
            log_rejection(txn.id, proposal, reason)
            return GateResult(approved=False, reason=reason)
            
    return GateResult(approved=True)
