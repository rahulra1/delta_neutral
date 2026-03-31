import hashlib
import hmac


def generate_signature(secret, message):
    return hmac.new(
        bytes(secret, 'utf-8'),
        bytes(message, 'utf-8'),
        hashlib.sha256
    ).hexdigest()
