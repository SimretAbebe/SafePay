import hashlib
import hmac
import time
from typing import Any, Optional, Union


def verify_webhook(
    secret: Union[str, bytes],
    timestamp: Any,
    signature: str,
    body: Union[str, bytes],
    tolerance_seconds: int = 300,
    now: Optional[Union[int, float]] = None,
) -> bool:
    """
    Verifies that a received webhook payload matches the timestamped HMAC-SHA256 signature
    and was dispatched within the acceptable tolerance window.

    :param secret: The shared WEBHOOK_SECRET.
    :param timestamp: The integer UNIX timestamp string from X-SafePay-Timestamp.
    :param signature: The hex digest from X-SafePay-Signature.
    :param body: The raw request body as bytes or string.
    :param tolerance_seconds: Max acceptable age or future offset in seconds (default 300 = 5m).
    :param now: Optional override for current UNIX timestamp (used for testing).
    :return: True if valid, False otherwise.
    """
    if not secret or not timestamp or not signature or body is None:
        return False

    # 1. Reject when timestamp is not an integer
    try:
        if isinstance(timestamp, bool):
            return False
        # Ensure it represents an integer without decimal fractions
        if isinstance(timestamp, str) and not timestamp.strip().lstrip("-").isdigit():
            return False
        ts_int = int(timestamp)
    except (ValueError, TypeError):
        return False

    # 2. Check tolerance window
    current_time = float(now) if now is not None else time.time()
    if tolerance_seconds is not None:
        if abs(current_time - ts_int) > tolerance_seconds:
            return False

    # 3. Recompute HMAC of f"{timestamp}.{body}"
    try:
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        body_bytes = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        to_sign = f"{ts_int}.".encode("utf-8") + body_bytes

        expected_signature = hmac.new(secret_bytes, to_sign, hashlib.sha256).hexdigest()

        if not isinstance(signature, str):
            return False

        return hmac.compare_digest(expected_signature, signature)
    except Exception:
        return False
