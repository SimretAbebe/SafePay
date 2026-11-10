"""
Management command: ledger_verify

Checks the integrity of the double-entry ledger.

Checks performed:
  1. Every journal is balanced (sum debits == sum credits).
  2. Global totals: total debits == total credits per (livemode, currency).
  3. Every succeeded payment has exactly one payment_succeeded journal
     (skipped with --allow-missing).
  4. Each payment journal's entry amounts equal its payment's amount.
  5. No entry exists without a journal.
  6. Every succeeded refund has exactly one refund_succeeded journal
     matching its amount, and non-succeeded refunds have none.

Exit code 1 if any check fails.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum, Q, F

from payments.ledger.models import LedgerEntry, LedgerJournal
from payments.models import Payment, Refund


class Command(BaseCommand):
    help = "Verify integrity of the double-entry ledger."

    def add_arguments(self, parser):
        parser.add_argument(
            "--allow-missing",
            action="store_true",
            default=False,
            help="Print count of succeeded payments without a journal instead of failing.",
        )

    def handle(self, *args, **options):
        allow_missing = options["allow_missing"]
        failed = False
        results = []

        # ── Check 1: every journal is balanced ──────────────────────────
        unbalanced = []
        for journal in LedgerJournal.objects.all():
            debits = (
                journal.entries.filter(side="debit").aggregate(s=Sum("amount"))["s"]
                or Decimal("0")
            )
            credits = (
                journal.entries.filter(side="credit").aggregate(s=Sum("amount"))["s"]
                or Decimal("0")
            )
            if debits != credits:
                unbalanced.append(journal.pk)

        if unbalanced:
            results.append(("Journals balanced", "FAIL", f"{len(unbalanced)} unbalanced: {unbalanced}"))
            failed = True
        else:
            count = LedgerJournal.objects.count()
            results.append(("Journals balanced", "OK", f"{count} journals checked"))

        # ── Check 2: global debits == credits per (livemode, currency) ──
        global_ok = True
        entries_qs = LedgerEntry.objects.values(
            "account__livemode", "account__currency"
        ).annotate(
            total_debit=Sum("amount", filter=Q(side="debit")),
            total_credit=Sum("amount", filter=Q(side="credit")),
        )
        for row in entries_qs:
            td = row["total_debit"] or Decimal("0")
            tc = row["total_credit"] or Decimal("0")
            if td != tc:
                results.append((
                    f"Global balance (live={row['account__livemode']}, {row['account__currency']})",
                    "FAIL",
                    f"debits={td}, credits={tc}",
                ))
                global_ok = False
                failed = True
        if global_ok:
            results.append(("Global balance", "OK", "debits == credits in all modes/currencies"))

        # ── Check 3: every succeeded payment has a journal ──────────────
        succeeded_ids = set(
            Payment.objects.filter(status="succeeded", merchant__isnull=False).values_list("id", flat=True)
        )
        journaled_ids = set(
            LedgerJournal.objects.filter(kind="payment_succeeded").values_list(
                "payment_id", flat=True
            )
        )
        missing = succeeded_ids - journaled_ids
        if missing:
            if allow_missing:
                results.append((
                    "Succeeded payments journaled",
                    "SKIP",
                    f"{len(missing)} missing (--allow-missing)",
                ))
            else:
                results.append((
                    "Succeeded payments journaled",
                    "FAIL",
                    f"{len(missing)} succeeded payments without a journal",
                ))
                failed = True
        else:
            results.append((
                "Succeeded payments journaled",
                "OK",
                f"{len(succeeded_ids)} payments checked",
            ))

        # ── Check 4: each journal's amounts match its payment's amount ──
        amount_mismatches = []
        for journal in LedgerJournal.objects.filter(kind="payment_succeeded").select_related("payment").prefetch_related("entries"):
            for entry in journal.entries.all():
                if entry.amount != journal.payment.amount:
                    amount_mismatches.append(journal.pk)
                    break
        if amount_mismatches:
            results.append((
                "Journal amounts match payment",
                "FAIL",
                f"{len(amount_mismatches)} journals with mismatched amounts: {amount_mismatches}",
            ))
            failed = True
        else:
            results.append(("Journal amounts match payment", "OK", "all match"))

        # ── Check 5: no orphan entries ──────────────────────────────────
        orphan_entries = LedgerEntry.objects.filter(journal__isnull=True).count()
        # Also check for entries whose journal_id doesn't exist
        entry_journal_ids = set(
            LedgerEntry.objects.values_list("journal_id", flat=True)
        )
        existing_journal_ids = set(
            LedgerJournal.objects.values_list("id", flat=True)
        )
        orphan_refs = entry_journal_ids - existing_journal_ids
        total_orphans = orphan_entries + len(orphan_refs)

        if total_orphans:
            results.append(("No orphan entries", "FAIL", f"{total_orphans} orphan entries"))
            failed = True
        else:
            results.append(("No orphan entries", "OK", f"{LedgerEntry.objects.count()} entries checked"))

        # ── Check 6: every succeeded refund has exactly one journal matching its amount ──
        refund_errors = []
        succeeded_refunds = Refund.objects.filter(status="succeeded", payment__merchant__isnull=False)
        for refund in succeeded_refunds:
            r_journals = list(LedgerJournal.objects.filter(refund=refund, kind="refund_succeeded").prefetch_related("entries"))
            if len(r_journals) != 1:
                refund_errors.append(f"Refund {refund.pk} has {len(r_journals)} journals (expected 1)")
                continue
            r_journal = r_journals[0]
            for entry in r_journal.entries.all():
                if entry.amount != refund.amount:
                    refund_errors.append(
                        f"Refund {refund.pk} journal {r_journal.pk} entry amount {entry.amount} != refund amount {refund.amount}"
                    )
                    break

        # Non-succeeded refunds must have none
        bad_refund_journals = list(
            LedgerJournal.objects.filter(
                refund__status__in=["pending", "failed", "unsupported"],
                kind="refund_succeeded",
            ).values_list("refund_id", flat=True)
        )
        if bad_refund_journals:
            refund_errors.append(f"Non-succeeded refunds have journals: {bad_refund_journals}")

        # Orphan refund journals without a refund FK
        orphan_refund_journals = LedgerJournal.objects.filter(kind="refund_succeeded", refund__isnull=True).count()
        if orphan_refund_journals:
            refund_errors.append(f"{orphan_refund_journals} refund journals without a refund")

        if refund_errors:
            results.append((
                "Succeeded refunds journaled",
                "FAIL",
                f"{len(refund_errors)} errors: {'; '.join(refund_errors[:3])}",
            ))
            failed = True
        else:
            results.append((
                "Succeeded refunds journaled",
                "OK",
                f"{succeeded_refunds.count()} refunds checked",
            ))

        # ── Print table ─────────────────────────────────────────────────
        self.stdout.write("")
        self.stdout.write(f"{'Check':<40} {'Result':<6} {'Detail'}")
        self.stdout.write("-" * 80)
        for check_name, status, detail in results:
            self.stdout.write(f"{check_name:<40} {status:<6} {detail}")
        self.stdout.write("")

        if failed:
            self.stderr.write(self.style.ERROR("LEDGER VERIFY: FAILED"))
            raise SystemExit(1)
        else:
            self.stdout.write(self.style.SUCCESS("LEDGER VERIFY: OK"))

