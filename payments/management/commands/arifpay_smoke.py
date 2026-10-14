import time
from decimal import Decimal
from django.conf import settings
from django.core.management.base import BaseCommand
from payments.models import Payment
from payments.providers import ArifPayProvider, ProviderError, get_provider


class Command(BaseCommand):
    help = (
        "ArifPay manual smoke test helper.\n\n"
        "Two modes:\n"
        "  init          -- Creates a HOSTED CHECKOUT session (production flow).\n"
        "                   Requires a real merchant API key (not the portal key).\n"
        "  init --pss    -- Runs a SANDBOX PSS direct transfer (testing only).\n"
        "                   Uses the portal simulator key (arif-sandbox-portal-key).\n"
        "                   Does NOT produce a checkout URL.\n"
        "  verify --ref  -- Verifies a hosted checkout session by sessionId.\n"
        "                   Requires a real merchant API key.\n"
        "  providers     -- Lists all registered payment providers in SafePay.\n"
        "  webhook       -- Simulates an incoming ArifPay webhook callback.\n"
        "  audit         -- Displays recent tamper-proof inbound audit logs."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "action",
            choices=["init", "verify", "providers", "webhook", "audit"],
            help="Action to run.",
        )
        parser.add_argument(
            "--pss",
            action="store_true",
            default=False,
            help=(
                "Sandbox PSS mode: call the direct PSS transfer endpoint instead of "
                "creating a hosted checkout session. Only valid with 'init'. "
                "Works with ARIFPAY_API_KEY=arif-sandbox-portal-key."
            ),
        )
        parser.add_argument(
            "--amount",
            type=Decimal,
            default=Decimal("100.00"),
            help="Payment amount (default: 100.00)",
        )
        parser.add_argument(
            "--email",
            type=str,
            default="customer@gmail.com",
            help="Customer email (default: customer@gmail.com)",
        )
        parser.add_argument(
            "--phone",
            type=str,
            default="",
            help="Customer phone (optional, used by sandbox PSS)",
        )
        parser.add_argument(
            "--item-name",
            type=str,
            default="SafePay Test Item",
            help="Item name (default: SafePay Test Item)",
        )
        parser.add_argument(
            "--sim-mode",
            type=str,
            default="SUCCESS",
            choices=["SUCCESS", "FAIL"],
            help="Sandbox PSS simulation mode (default: SUCCESS). Only used with --pss.",
        )
        parser.add_argument(
            "--fee-source",
            type=str,
            default="CUSTOMER",
            choices=["CUSTOMER", "MERCHANT"],
            help="Sandbox PSS fee source (default: CUSTOMER). Only used with --pss.",
        )
        parser.add_argument(
            "--ref",
            type=str,
            default="",
            help="Provider reference (sessionId) to verify.",
        )

    def handle(self, *args, **options):
        action = options["action"]
        use_pss = options["pss"]

        # ------------------------------------------------------------------ #
        # providers: list registered providers and capabilities
        # ------------------------------------------------------------------ #
        if action == "providers":
            from payments.providers.registry import PROVIDER_REGISTRY
            self.stdout.write(self.style.SUCCESS("\n[REGISTERED PAYMENT PROVIDERS]"))
            self.stdout.write(f"  {'Provider':<12} {'Supports Cancel':<18} {'Status'}")
            self.stdout.write("  " + "-" * 42)
            for name, p in sorted(PROVIDER_REGISTRY.items()):
                self.stdout.write(f"  {name:<12} {str(p.supports_cancel):<18} ACTIVE")
            self.stdout.write("")
            return

        # ------------------------------------------------------------------ #
        # audit: display recent inbound webhook audit events
        # ------------------------------------------------------------------ #
        if action == "audit":
            from payments.models import InboundWebhookEvent
            self.stdout.write(self.style.SUCCESS("\n[RECENT INBOUND WEBHOOK AUDIT LOG]"))
            self.stdout.write(f"  {'ID':<5} {'Provider':<10} {'Outcome':<22} {'Sig Valid':<10} {'SHA-256 Hash'}")
            self.stdout.write("  " + "-" * 68)
            events = list(InboundWebhookEvent.objects.filter(provider="arifpay").order_by("-received_at")[:5])
            if not events:
                self.stdout.write("  (No ArifPay audit events recorded yet)")
            else:
                for e in events:
                    self.stdout.write(f"  {e.id:<5} {e.provider:<10} {e.outcome:<22} {str(e.signature_valid):<10} {e.body_sha256[:16]}...")
            self.stdout.write("")
            return

        # ------------------------------------------------------------------ #
        # webhook: simulate an inbound webhook callback
        # ------------------------------------------------------------------ #
        if action == "webhook":
            import json
            from django.test import Client
            from payments.models import InboundWebhookEvent

            c = Client()
            test_sess = options.get("ref") or "demo-arif-session-100"
            payload = {"sessionId": test_sess, "transaction": {"transactionStatus": "SUCCESS"}}
            res = c.post(
                "/api/webhooks/arifpay/",
                json.dumps(payload),
                content_type="application/json",
            )
            self.stdout.write(self.style.SUCCESS("\n[INBOUND WEBHOOK SIMULATION]"))
            self.stdout.write(f"  Endpoint : POST /api/webhooks/arifpay/")
            self.stdout.write(f"  Session  : {test_sess}")
            self.stdout.write(f"  HTTP Code: {res.status_code}")
            self.stdout.write(f"  Response : {res.json()}")

            event = InboundWebhookEvent.objects.filter(provider="arifpay").order_by("-received_at").first()
            if event:
                self.stdout.write(self.style.SUCCESS("\n[INBOUND AUDIT EVENT RECORDED]"))
                self.stdout.write(f"  Event ID  : #{event.id}")
                self.stdout.write(f"  Outcome   : {event.outcome}")
                self.stdout.write(f"  Sig Valid : {event.signature_valid}")
                self.stdout.write(f"  SHA-256   : {event.body_sha256[:24]}... (zero sensitive PII stored)\n")
            return

        api_key = getattr(settings, "ARIFPAY_API_KEY", "") or ""
        if not api_key.strip():
            self.stderr.write(
                self.style.ERROR(
                    "ARIFPAY_API_KEY is not set. Add it to your .env file."
                )
            )
            return

        # ------------------------------------------------------------------ #
        # init: hosted checkout OR sandbox PSS transfer
        # ------------------------------------------------------------------ #
        if action == "init":
            amount = options["amount"]
            email = options["email"]
            phone = options.get("phone", "")
            item_name = options.get("item_name", "SafePay Test Item")
            idempotency_key = f"arifpay-smoke-{int(time.time() * 1000)}"

            payment = Payment.objects.create(
                merchant=None,
                idempotency_key=idempotency_key,
                amount=amount,
                sender="smoke",
                receiver="arifpay",
            )
            payment.customer_email = email
            payment.item_name = item_name
            if phone:
                payment.customer_phone = phone

            provider = ArifPayProvider()

            if use_pss:
                # ---- Sandbox PSS direct transfer ----
                is_sandbox = getattr(settings, "ARIFPAY_SANDBOX", True)
                if not is_sandbox:
                    self.stderr.write(
                        self.style.ERROR(
                            "Sandbox PSS (--pss) requires ARIFPAY_SANDBOX=True in your .env."
                        )
                    )
                    return

                sim_mode = options["sim_mode"]
                fee_source = options["fee_source"]

                self.stdout.write(self.style.WARNING(
                    "\n[SANDBOX PSS MODE]\n"
                    f"  Endpoint : https://gateway.arifpay.org/api/sandbox/c2b/pss/direct/transfer\n"
                    f"  Sim mode : {sim_mode}\n"
                    f"  Fee src  : {fee_source}\n"
                    "  NOTE: This is a sandbox-only direct PSS transfer, NOT a hosted checkout.\n"
                    "        No checkout URL is produced. This tests API connectivity only.\n"
                ))

                try:
                    result = provider.sandbox_pss_transfer(
                        payment,
                        sim_mode=sim_mode,
                        fee_source=fee_source,
                    )
                except ProviderError as e:
                    self.stderr.write(self.style.ERROR(f"ArifPay sandbox PSS failed: {e}"))
                    return

                payment.provider = "arifpay"
                payment.provider_reference = result.provider_reference
                payment.save(update_fields=["provider", "provider_reference", "updated_at"])

                self.stdout.write(self.style.SUCCESS("Sandbox PSS transfer completed!"))
                self.stdout.write(f"  Payment ID        : {payment.id}")
                self.stdout.write(f"  Provider Reference: {result.provider_reference}")
                self.stdout.write(f"  SafePay Status    : {result.status}")

            else:
                # ---- Hosted checkout (production flow) ----
                self.stdout.write(self.style.WARNING(
                    "\n[HOSTED CHECKOUT MODE]\n"
                    "  Endpoint: POST /api/checkout/session\n"
                    "  Requires a MERCHANT API KEY (not the portal simulator key).\n"
                    "  Use 'arifpay_smoke init --pss' to test with arif-sandbox-portal-key.\n"
                ))

                try:
                    result = provider.initialize(payment)
                except ProviderError as e:
                    self.stderr.write(self.style.ERROR(f"ArifPay initialization failed: {e}"))
                    return

                payment.provider = "arifpay"
                payment.provider_reference = result.provider_reference
                payment.checkout_url = result.checkout_url
                payment.save(update_fields=["provider", "provider_reference", "checkout_url", "updated_at"])

                self.stdout.write(self.style.SUCCESS("ArifPay hosted checkout session created!"))
                self.stdout.write(f"  Payment ID        : {payment.id}")
                self.stdout.write(f"  Session ID        : {result.provider_reference}")
                self.stdout.write(f"  Checkout URL      : {result.checkout_url}")

        # ------------------------------------------------------------------ #
        # verify: hosted checkout session status
        # ------------------------------------------------------------------ #
        elif action == "verify":
            if use_pss:
                self.stderr.write(self.style.ERROR(
                    "--pss is not applicable to 'verify'. "
                    "Verification always uses the hosted checkout session endpoint."
                ))
                return

            ref = options.get("ref", "").strip()
            if not ref:
                self.stderr.write(self.style.ERROR(
                    "Error: --ref <session_id> is required for verify."
                ))
                return

            provider = ArifPayProvider()
            try:
                result = provider.verify(ref)
            except ProviderError as e:
                self.stderr.write(self.style.ERROR(f"ArifPay verification failed: {e}"))
                return

            provider_native_status = "unknown"
            if isinstance(result.raw, dict):
                tx = result.raw.get("transaction", {})
                if isinstance(tx, dict):
                    provider_native_status = tx.get("transactionStatus", "unknown")

            self.stdout.write(self.style.SUCCESS("ArifPay session verified!"))
            self.stdout.write(f"  Provider Reference    : {result.provider_reference}")
            self.stdout.write(f"  SafePay Status        : {result.status}")
            self.stdout.write(f"  Provider Native Status: {provider_native_status}")
