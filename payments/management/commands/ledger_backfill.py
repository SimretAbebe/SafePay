"""
Management command: ledger_backfill

For succeeded payments that do not yet have a payment_succeeded journal,
call post_payment_succeeded() idempotently in batches of 200.

A second run is a no-op.
"""

from django.core.management.base import BaseCommand
from django.db.models import Q

from payments.ledger.models import LedgerJournal
from payments.ledger.services import post_payment_succeeded
from payments.models import Payment


class Command(BaseCommand):
    help = "Backfill ledger journals for succeeded payments that are missing one."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Print how many would be created without creating anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        batch_size = 200

        # Succeeded payments with a merchant missing a payment_succeeded journal
        missing_qs = (
            Payment.objects.filter(status="succeeded", merchant__isnull=False)
            .exclude(ledger_journals__kind="payment_succeeded")
            .order_by("id")
        )

        total_missing = missing_qs.count()

        if dry_run:
            self.stdout.write(
                f"Dry run: {total_missing} journal(s) would be created."
            )
            return

        created = 0
        last_id = 0
        while True:
            batch = list(missing_qs.filter(id__gt=last_id)[:batch_size])
            if not batch:
                break
            for payment in batch:
                result = post_payment_succeeded(payment)
                if result is not None:
                    created += 1
                last_id = payment.id

        self.stdout.write(
            self.style.SUCCESS(
                f"Backfill complete: {created} journal(s) created "
                f"({total_missing} were missing)."
            )
        )

