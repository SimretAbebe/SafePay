import hashlib
import secrets
import uuid
from django.db import models


class Merchant(models.Model):
    name = models.CharField(max_length=255)
    api_key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    key_prefix = models.CharField(max_length=8)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @staticmethod
    def hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    @classmethod
    def generate_key_pair(cls):
        raw_key = secrets.token_urlsafe(32)
        api_key_hash = cls.hash_key(raw_key)
        key_prefix = raw_key[:8]
        return raw_key, api_key_hash, key_prefix

    @classmethod
    def create_with_key(cls, name: str, is_active: bool = True, **kwargs):
        raw_key, api_key_hash, key_prefix = cls.generate_key_pair()
        merchant = cls.objects.create(
            name=name,
            api_key_hash=api_key_hash,
            key_prefix=key_prefix,
            is_active=is_active,
            **kwargs,
        )
        return merchant, raw_key

    def __str__(self):
        return self.name


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
        ("cancelled", "Cancelled"),
    ]

    VALID_TRANSITIONS = {
        "pending": ["processing", "cancelled"],
        "processing": ["succeeded", "failed"],
        "succeeded": [],
        "failed": [],
        "cancelled": [],
    }

    merchant = models.ForeignKey(
        Merchant,
        on_delete=models.PROTECT,
        related_name="payments",
        null=True,
        blank=True,
    )

    idempotency_key = models.CharField(
        max_length=255,
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

        old_status = self.status
        self.status = new_status
        self.save(update_fields=["status", "updated_at"])

        PaymentStatusHistory.objects.create(
            payment=self,
            from_status=old_status,
            to_status=new_status,
        )

    @property
    def history(self):
        return self.status_history.all()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["merchant", "idempotency_key"],
                name="unique_idempotency_key_per_merchant",
            )
        ]

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

    event_id = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )
    event_type = models.CharField(max_length=50)

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


class PaymentStatusHistory(models.Model):
    payment = models.ForeignKey(
        Payment,
        on_delete=models.CASCADE,
        related_name="status_history",
    )
    from_status = models.CharField(
        max_length=20,
        choices=Payment.STATUS_CHOICES,
    )
    to_status = models.CharField(
        max_length=20,
        choices=Payment.STATUS_CHOICES,
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["changed_at"]
        verbose_name_plural = "Payment status histories"

    def __str__(self):
        return f"{self.payment.idempotency_key}: {self.from_status} -> {self.to_status}"