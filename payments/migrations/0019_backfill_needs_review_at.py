"""Data migration: backfill needs_review_at for payments already flagged."""

from django.db import migrations


def backfill_needs_review_at(apps, schema_editor):
    Payment = apps.get_model("payments", "Payment")
    # For every payment already flagged for review that has no needs_review_at,
    # set needs_review_at to last_synced_at (preferred) or updated_at.
    flagged = Payment.objects.filter(needs_review=True, needs_review_at__isnull=True)
    for payment in flagged.iterator():
        payment.needs_review_at = payment.last_synced_at or payment.updated_at
        payment.save(update_fields=["needs_review_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0018_review_queue_fields_and_proxy"),
    ]

    operations = [
        migrations.RunPython(backfill_needs_review_at, migrations.RunPython.noop),
    ]
