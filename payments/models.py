from django.db import models


class Payment(models.Model):
    """
    One row per payment attempt. The idempotency_key's unique=True is the
    entire safety mechanism for this project -- it's enforced by
    PostgreSQL itself, not just checked in Python.
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("succeeded", "Succeeded"),
        ("failed", "Failed"),
    ]

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

class InvalidStateTransition(Exception):
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
        "succeeded": [],  # terminal state 
        "failed": [],      # terminal state
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