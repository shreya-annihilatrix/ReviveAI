import hashlib


def make_idempotency_key(txn_id, attempt_no, action_type):
    value = f"{txn_id}{attempt_no}{action_type}"

    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()