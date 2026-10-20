"""
payments/alerts.py
Helpers for review-queue alerting: message formatting, delivery,
and cooldown-based scheduling.
"""

import logging

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_age(td):
    """Return a short human-readable age string from a timedelta.

    Examples: "0m", "12m", "3h 12m", "2d 4h".
    """
    total_seconds = int(td.total_seconds())
    if total_seconds < 0:
        total_seconds = 0

    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def send_alert_message(text):
    """Send *text* to every enabled alert channel.

    * Always writes the text to the logger at WARNING level.
    * If ``TELEGRAM_BOT_TOKEN`` **and** ``TELEGRAM_CHAT_ID`` are both set,
      also POSTs to the Telegram Bot API ``sendMessage`` endpoint.
    * Returns ``True`` only when every enabled channel succeeded.
    * On any error the token, URL, and response body are **never** logged.
    """
    logger.warning("Review-queue alert: %s", text)

    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        # Logger-only mode — counts as delivered.
        return True

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
        if 200 <= resp.status_code < 300:
            return True
        # Non-2xx — log a safe message WITHOUT the token, URL or body.
        logger.error(
            "Telegram alert delivery failed (HTTP %s)", resp.status_code,
        )
        return False
    except Exception:
        logger.error("Telegram alert delivery failed (exception)")
        return False


# ---------------------------------------------------------------------------
# Cooldown scheduling
# ---------------------------------------------------------------------------

def schedule_review_alert():
    """Dispatch alert_new_reviews exactly once per cooldown window.

    Uses ``cache.add`` so only the first caller in the window wins.
    Called from ``transaction.on_commit`` in services.reconcile_one.
    """
    cooldown = getattr(settings, "REVIEW_ALERT_COOLDOWN_SECONDS", 300)
    if cache.add("review_alert_cooldown", True, cooldown):
        from .tasks import alert_new_reviews
        alert_new_reviews.delay()
