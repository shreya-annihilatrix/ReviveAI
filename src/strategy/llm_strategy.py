"""
LLM Action Proposal Schema — Phase 6.

This module defines the structured output schema for the LLM when
acting as the Strategy Agent. The LLM NEVER executes actions directly;
it only proposes them by populating this schema.
"""

from datetime import datetime
from pydantic import BaseModel, Field

class ActionProposal(BaseModel):
    action_type: str = Field(
        description="The type of action to take. Must be in allowlist "
                    "(e.g., retry, payment_link, sms_reminder, whatsapp, "
                    "discount_10pct, human_escalation, split_payment, do_nothing)"
    )
    instrument: str = Field(
        description="The payment instrument to use (e.g., upi, card, netbanking)"
    )
    amount: float = Field(
        description="The amount to attempt recovery for. Policy Gate ensures this <= original."
    )
    schedule_at: datetime = Field(
        description="When to execute this action. Policy Gate ensures this is within [now, now+7d]."
    )
    channel: str = Field(
        description="The communication channel to use (e.g., sms, email, whatsapp, none)."
    )
    message_body: str = Field(
        description="The message to send to the customer, in their preferred language."
    )
    rationale: str = Field(
        description="A clear, human-readable explanation of why this action was chosen."
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Confidence in this proposal (0.0 to 1.0)."
    )
