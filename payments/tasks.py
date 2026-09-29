import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def log_payment_event(payment_id, event_description):
    logger.info(f"[Celery task ran] Payment {payment_id}: {event_description}")
    return f"Logged: {event_description}"