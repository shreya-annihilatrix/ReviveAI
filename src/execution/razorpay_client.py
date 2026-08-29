import os
import razorpay
from datetime import datetime, timedelta, timezone

def get_real_client():
    key_id = os.getenv("RAZORPAY_KEY_ID", "")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET", "")
    return razorpay.Client(auth=(key_id, key_secret))

def now():
    return datetime.now(timezone.utc)

def create_payment_link(txn, idempotency_key):
    client = get_real_client()
    # Handle if txn is a dict or an object
    amount = getattr(txn, "amount", 0) if not isinstance(txn, dict) else txn.get("amount", 0)
    txn_id = getattr(txn, "id", 0) if not isinstance(txn, dict) else txn.get("id", 0)
    
    # Check if amount is None
    if amount is None:
        amount = 0
        
    return client.payment_link.create({
        "amount": int(amount * 100),  # paise
        "currency": "INR",
        "description": f"Recovery for order {txn_id}",
        "customer": {"name": "Customer", "email": "customer@example.com"},
        "expire_by": int((now() + timedelta(hours=48)).timestamp()),
        "reminder_enable": False,
    }, headers={"X-Idempotency-Key": idempotency_key})

def create_order(txn, idempotency_key):
    client = get_real_client()
    amount = getattr(txn, "amount", 0) if not isinstance(txn, dict) else txn.get("amount", 0)
    txn_id = getattr(txn, "id", 0) if not isinstance(txn, dict) else txn.get("id", 0)
    
    if amount is None:
        amount = 0
        
    return client.order.create({
        "amount": int(amount * 100),
        "currency": "INR",
        "receipt": str(txn_id),
    }, headers={"X-Idempotency-Key": idempotency_key})

def capture_payment(payment_id, amount):
    client = get_real_client()
    return client.payment.capture(payment_id, int(amount * 100), {"currency": "INR"})

