from django.db import models


class InvalidStateTransition(Exception):
    """
    Raised when code tries to move a Payment into a status it's not
    allowed to move into from its current status.
    """
    pass


class Payment(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("succeeded", "Succeeded"),
        ("failed", "Failed"),
    ]

    VALID_TRANSITIONS = {
        "pending": ["processing"],
        "processing": ["succeeded", "failed"],
        "succeeded": [],
        "failed": [],
    }

    idempotency_key = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Client-provided unique key. Same key sent twice must "
                   "never result in two payments.",
    )

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    sender = models.CharField(max_length=255)
    receiver = models.CharField(max_length=255)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def transition_to(self, new_status):
        allowed_next_statuses = self.VALID_TRANSITIONS.get(self.status, [])

        if new_status not in allowed_next_statuses:
            raise InvalidStateTransition(
                f"Cannot move payment {self.idempotency_key} from "
                f"'{self.status}' to '{new_status}'. Allowed next "
                f"states from '{self.status}': {allowed_next_statuses or 'none (terminal state)'}"
            )

        self.status = new_status
        self.save(update_fields=["status", "updated_at"])

    def __str__(self):
        return f"{self.idempotency_key} - {self.status} - {self.amount}"


class WebhookDelivery(models.Model):

    STATUS_CHOICES = [
        ("pending", "Pending"),     
        ("succeeded", "Succeeded"), 
        ("failed", "Failed"),       
    ]

    payment = models.ForeignKey(
        Payment,
        on_delete=models.CASCADE,
        related_name="webhook_deliveries",
    )

    target_url = models.URLField()
    payload = models.JSONField()

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
    )

    attempt_count = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)

    last_response_code = models.IntegerField(null=True, blank=True)
    last_error = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Webhook for payment {self.payment_id} -> {self.status} ({self.attempt_count}/{self.max_attempts})"