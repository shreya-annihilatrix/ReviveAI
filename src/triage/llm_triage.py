"""
LLM-based triage — Tier 2 (Haiku) and Tier 3 (Sonnet).

Every call:
  - Uses Claude tool_use for guaranteed structured output
  - Logs cost to cost_log table
  - Returns a TriageOutput Pydantic model

Tier selection:
  - Default → Haiku  (fast, cheap)
  - Escalate → Sonnet when amount > 10000 OR haiku confidence < 0.65
"""

import json
import logging
import os
import time
from typing import Literal

import anthropic
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

HAIKU_MODEL  = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"

# USD per 1M tokens (approximate)
_PRICING = {
    HAIKU_MODEL:  {"input": 0.80,  "output": 4.00},
    SONNET_MODEL: {"input": 3.00,  "output": 15.00},
}

ESCALATION_AMOUNT_THRESHOLD    = 10_000.0   # ₹
ESCALATION_CONFIDENCE_THRESHOLD = 0.65


# ---------------------------------------------------------------------------
# Pydantic output model
# ---------------------------------------------------------------------------

class TriageOutput(BaseModel):
    failure_type: Literal[
        "insufficient_funds",
        "bank_degradation",
        "vpa_invalid",
        "auth_failure",
        "limit_exceeded",
        "expired_instrument",
        "mandate_failure",
        "unknown",
    ]
    root_cause: str = Field(description="Short technical root cause string")
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_channel: str = Field(
        description="e.g. payment_link, retry_same, reauth_flow, split_payment"
    )
    reasoning: str = Field(description="Explainable AI rationale — shown in dashboard")

    @field_validator("confidence")
    @classmethod
    def round_confidence(cls, v: float) -> float:
        return round(v, 4)


# ---------------------------------------------------------------------------
# Tool schema (forces structured output from Claude)
# ---------------------------------------------------------------------------

_TRIAGE_TOOL = {
    "name": "submit_triage",
    "description": "Submit the structured triage diagnosis for a failed payment transaction.",
    "input_schema": {
        "type": "object",
        "properties": {
            "failure_type": {
                "type": "string",
                "enum": [
                    "insufficient_funds", "bank_degradation", "vpa_invalid",
                    "auth_failure", "limit_exceeded", "expired_instrument",
                    "mandate_failure", "unknown",
                ],
                "description": "Primary failure classification",
            },
            "root_cause": {
                "type": "string",
                "description": "Short technical root cause (snake_case)",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Confidence in the classification (0-1)",
            },
            "recommended_channel": {
                "type": "string",
                "description": "Recovery channel: payment_link | retry_same | retry_2h_window | update_vpa_flow | payment_method_update | reauth_flow | split_payment | wait_and_retry | salary_window_retry",
            },
            "reasoning": {
                "type": "string",
                "description": "2-3 sentence explanation for the dashboard",
            },
        },
        "required": ["failure_type", "root_cause", "confidence",
                     "recommended_channel", "reasoning"],
    },
}

_SYSTEM_PROMPT = """You are a payment failure triage specialist for an Indian fintech platform.
Given a failed transaction's details, classify the failure precisely.

Rules:
- Be specific about root cause — "upi_daily_limit" not just "limit"
- confidence should reflect how certain you are given the available information
- recommended_channel must be one of the enum values
- reasoning should be 2-3 sentences suitable for a business dashboard
- NEVER suggest modifying the transaction amount
- NEVER contact opted-out customers
"""


def _build_user_prompt(txn_data: dict) -> str:
    return f"""Triage this failed payment transaction:

Transaction ID      : {txn_data.get('txn_id', 'N/A')}
Amount              : Rs.{txn_data.get('amount', 0):,.2f}
Payment Method      : {txn_data.get('payment_method', 'unknown')}
Bank                : {txn_data.get('bank', 'unknown')}
Failure Code        : {txn_data.get('failure_code', 'unknown')}
Failure Reason      : {txn_data.get('failure_reason', 'N/A')}
Customer CLV        : Rs.{txn_data.get('customer_lifetime_value', 0):,.0f}
Prev Successful     : {txn_data.get('previous_successful_payments', 0)}
Prev Failed         : {txn_data.get('previous_failed_payments', 0)}
Prev Recoveries     : {txn_data.get('previous_recoveries', 0)}
Salary Window       : {txn_data.get('inferred_salary_window', 'unknown')}
Mandate Expiry      : {txn_data.get('mandate_expiry', 'N/A')}
Customer Opted Out  : {txn_data.get('opted_out', False)}
Order Notes         : {txn_data.get('order_notes', 'none')}

Call submit_triage with your structured diagnosis."""


# ---------------------------------------------------------------------------
# Core LLM call
# ---------------------------------------------------------------------------

def _call_claude(
    model: str,
    txn_data: dict,
    db,
    txn_id: str = "unknown",
) -> TriageOutput:
    """
    Call Claude with tool_use, parse into TriageOutput, log cost.
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    messages = [{"role": "user", "content": _build_user_prompt(txn_data)}]

    t0 = time.time()
    response = client.messages.create(
        model=model,
        max_tokens=512,
        system=_SYSTEM_PROMPT,
        tools=[_TRIAGE_TOOL],
        tool_choice={"type": "tool", "name": "submit_triage"},
        messages=messages,
    )
    latency_ms = (time.time() - t0) * 1000

    # Extract tool call result
    tool_block = next(
        (b for b in response.content if b.type == "tool_use"),
        None,
    )
    if tool_block is None:
        raise ValueError(f"Claude ({model}) did not call submit_triage")

    output = TriageOutput(**tool_block.input)

    # Log cost
    _log_cost(
        db=db,
        txn_id=txn_id,
        model=model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        latency_ms=latency_ms,
    )

    log.info(
        "LLM triage: model=%s txn=%s type=%s conf=%.2f latency=%.0fms",
        model, txn_id, output.failure_type, output.confidence, latency_ms,
    )
    return output


def _log_cost(db, txn_id: str, model: str, input_tokens: int,
              output_tokens: int, latency_ms: float) -> None:
    """Write a row to cost_log."""
    from src.data.database import CostLog

    pricing = _PRICING.get(model, {"input": 3.0, "output": 15.0})
    cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

    tier = "haiku" if "haiku" in model.lower() else "sonnet"
    row = CostLog(
        txn_id=str(txn_id),
        model_tier=tier,
        model_name=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=round(cost, 8),
        latency_ms=round(latency_ms, 2),
    )
    db.add(row)
    db.commit()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def triage_with_llm(
    txn_data: dict,
    db,
    txn_id: str = "unknown",
    force_sonnet: bool = False,
) -> tuple[TriageOutput, str]:
    """
    Run Tier 2 (Haiku) triage. Escalate to Tier 3 (Sonnet) if needed.

    Returns
    -------
    (TriageOutput, tier_used)   where tier_used is "haiku" or "sonnet"
    """
    amount   = float(txn_data.get("amount", 0))
    opted_out = txn_data.get("opted_out", False)

    if opted_out:
        # Skip LLM entirely — compliance hard-stop
        return TriageOutput(
            failure_type="unknown",
            root_cause="opted_out_customer",
            confidence=1.0,
            recommended_channel="do_nothing",
            reasoning=(
                "Customer has opted out of recovery communications. "
                "No action permitted per compliance rules."
            ),
        ), "rules"

    if force_sonnet:
        return _call_claude(SONNET_MODEL, txn_data, db, txn_id), "sonnet"

    # Tier 2 — Haiku
    haiku_output = _call_claude(HAIKU_MODEL, txn_data, db, txn_id)

    # Escalate to Sonnet?
    needs_sonnet = (
        haiku_output.confidence < ESCALATION_CONFIDENCE_THRESHOLD
        or amount > ESCALATION_AMOUNT_THRESHOLD
    )
    if needs_sonnet:
        log.info(
            "Escalating txn=%s to Sonnet (conf=%.2f amount=%.0f)",
            txn_id, haiku_output.confidence, amount,
        )
        return _call_claude(SONNET_MODEL, txn_data, db, txn_id), "sonnet"

    return haiku_output, "haiku"
