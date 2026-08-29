import re

def mask_pii(text: str) -> str:
    """Masks PII like VPA, Phone, Card, and UPI PINs."""
    if not text:
        return text
    # Mask VPA (e.g. abc@okhdfc -> ab***@okhdfc)
    text = re.sub(r'\b([a-zA-Z0-9]{2})[a-zA-Z0-9.\-_]*(@[a-zA-Z0-9]+)', r'\1***\2', text)
    # Mask Phone (e.g. 9876543210 -> 98765*****)
    text = re.sub(r'\b(\d{5})\d{5}\b', r'\1*****', text)
    # Mask Card (e.g. 4111 1111 1111 1111 -> 4111 **** **** 1111)
    text = re.sub(r'\b(\d{4})[\s\-]*\d{4}[\s\-]*\d{4}[\s\-]*(\d{4})\b', r'\1 **** **** \2', text)
    # Mask pure 16 digit card
    text = re.sub(r'\b(\d{4})\d{8}(\d{4})\b', r'\1********\2', text)
    # Mask UPI PIN (scrub if present)
    text = re.sub(r'(?i)(pin[\s:-]*)(\d{4,6})\b', r'\1****', text)
    return text

def sanitize_for_prompt(user_text: str) -> str:
    """Wraps customer-supplied data in XML tags to defend against prompt injection."""
    if not user_text:
        return "<customer_data></customer_data>"
    return f"<customer_data>{user_text}</customer_data>"
