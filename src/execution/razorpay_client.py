"""
Razorpay client wrapper.

In production  → uses real Razorpay SDK calls.
In test / mock → RAZORPAY_MOCK=true skips the network and
                 returns a deterministic fake response.

Every public method returns:
    {"success": bool, "razorpay_response": dict}

Idempotency key is passed through to the SDK so that a
duplicate call after a crash is safe.
"""

import os
import logging

log = logging.getLogger(__name__)

_MOCK = os.getenv("RAZORPAY_MOCK", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Mock backend
# ---------------------------------------------------------------------------

def _mock_dispatch(action_type: str, payload: dict, idempotency_key: str) -> dict:
    """Return a deterministic fake response for any action type."""
    log.debug("[MOCK] dispatch action=%s idem=%s", action_type, idempotency_key)
    return {
        "success": True,
        "razorpay_response": {
            "id": f"mock_{idempotency_key[:8]}",
            "action": action_type,
            "status": "created",
            "mock": True,
        },
    }


# ---------------------------------------------------------------------------
# Real Razorpay backend helpers
# ---------------------------------------------------------------------------

def _get_sdk_client():
    """Lazily import and construct the Razorpay SDK client."""
    import razorpay  # type: ignore
    key_id = os.getenv("RAZORPAY_KEY_ID", "")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET", "")
    return razorpay.Client(auth=(key_id, key_secret))


def _real_dispatch(action_type: str, payload: dict, idempotency_key: str) -> dict:
    """Dispatch via the real Razorpay API."""
    client = _get_sdk_client()

    if action_type in ("SEND_LINK", "RESEND_LINK"):
        resp = client.payment_link.create(
            {
                **payload,
                "idempotency_key": idempotency_key,
            }
        )
        return {"success": True, "razorpay_response": resp}

    # For SMS / email / WhatsApp nudges we call the notify endpoint
    if action_type in ("SEND_SMS", "SEND_EMAIL", "SEND_WHATSAPP"):
        payment_id = payload.get("razorpay_payment_id", "")
        medium = {
            "SEND_SMS": "sms",
            "SEND_EMAIL": "email",
            "SEND_WHATSAPP": "whatsapp",
        }[action_type]
        resp = client.payment.notifyBy(payment_id, medium)
        return {"success": True, "razorpay_response": resp}

    raise ValueError(f"Unknown action_type: {action_type}")


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

class RazorpayClient:
    """
    Thin wrapper around the Razorpay SDK.

    Instantiate once and reuse across the dispatcher loop.
    Honours the RAZORPAY_MOCK env-var at construction time.
    """

    def __init__(self, mock: bool | None = None):
        self._mock = _MOCK if mock is None else mock
        if self._mock:
            log.info("RazorpayClient: running in MOCK mode")
        else:
            log.info("RazorpayClient: running in LIVE mode")

    def dispatch(
        self,
        action_type: str,
        payload: dict,
        idempotency_key: str,
    ) -> dict:
        """
        Send a recovery action.

        Returns {"success": bool, "razorpay_response": dict}.
        Never raises — exceptions are caught and returned as
        {"success": False, "error": str}.
        """
        try:
            if self._mock:
                return _mock_dispatch(action_type, payload, idempotency_key)
            return _real_dispatch(action_type, payload, idempotency_key)
        except Exception as exc:  # noqa: BLE001
            log.exception("RazorpayClient.dispatch failed: %s", exc)
            return {"success": False, "error": str(exc), "razorpay_response": {}}
