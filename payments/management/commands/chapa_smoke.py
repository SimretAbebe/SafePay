import time
from decimal import Decimal
from django.conf import settings
from django.core.management.base import BaseCommand
from payments.models import Payment
from payments.providers import ProviderError, get_provider


class Command(BaseCommand):
    help = "Chapa test mode manual smoke test helper (init and verify)."

    def add_arguments(self, parser):
        parser.add_argument(
            "action",
            choices=["init", "verify"],
            help="Action to run: 'init' to initialize a checkout, or 'verify' to check status.",
        )
        parser.add_argument(
            "--amount",
            type=Decimal,
            default=Decimal("100.00"),
            help="Payment amount for init (default: 100.00)",
        )
        parser.add_argument(
            "--email",
            type=str,
            default="customer@gmail.com",
            help="Customer email for init (default: customer@gmail.com)",
        )
        parser.add_argument(
            "--ref",
            type=str,
            default="",
            help="Provider reference (tx_ref) to verify",
        )

    def handle(self, *args, **options):
        secret_key = getattr(settings, "CHAPA_SECRET_KEY", "") or ""
        if not secret_key.strip():
            self.stderr.write(
                self.style.ERROR(
                    "CHAPA_SECRET_KEY is not configured. Please set CHAPA_SECRET_KEY in your .env before running this command."
                )
            )
            return

        action = options.get("action")
        provider = get_provider("chapa")

        if action == "init":
            amount = options.get("amount")
            email = options.get("email", "customer@gmail.com")
            idempotency_key = f"chapa-smoke-{int(time.time() * 1000)}"

            payment = Payment.objects.create(
                merchant=None,
                idempotency_key=idempotency_key,
                amount=amount,
                sender="smoke",
                receiver="chapa",
            )
            payment.customer_email = email
            payment.customer_first_name = "Smoke"
            payment.customer_last_name = "User"

            try:
                result = provider.initialize(payment)
            except ProviderError as e:
                self.stderr.write(self.style.ERROR(f"Chapa initialization failed: {e}"))
                return

            payment.provider = "chapa"
            payment.provider_reference = result.provider_reference
            payment.checkout_url = result.checkout_url
            payment.save(update_fields=["provider", "provider_reference", "checkout_url", "updated_at"])

            self.stdout.write(self.style.SUCCESS("Chapa Payment initialized successfully!"))
            self.stdout.write(f"Payment ID: {payment.id}")
            self.stdout.write(f"Provider Reference: {result.provider_reference}")
            self.stdout.write(f"Checkout URL: {result.checkout_url}")

        elif action == "verify":
            ref = options.get("ref", "").strip()
            if not ref:
                self.stderr.write(self.style.ERROR("Error: --ref <provider_reference> is required for verify."))
                return

            try:
                result = provider.verify(ref)
            except ProviderError as e:
                self.stderr.write(self.style.ERROR(f"Chapa verification failed: {e}"))
                return

            provider_native_status = result.raw.get("status") if isinstance(result.raw, dict) else "unknown"
            self.stdout.write(self.style.SUCCESS("Chapa Payment verified successfully!"))
            self.stdout.write(f"Provider Reference: {result.provider_reference}")
            self.stdout.write(f"SafePay Status: {result.status}")
            self.stdout.write(f"Provider Native Status: {provider_native_status}")
