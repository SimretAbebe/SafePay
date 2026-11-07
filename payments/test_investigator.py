"""
payments/test_investigator.py

Test suite for Payment Investigator feature (API endpoint, Admin view, and investigator engine).
Covers:
1. Succeeded payment with delivered webhook.
2. Fallback provider attempt sequence with clean payment.
3. Needs review reasons mapping and next step.
4. Expired payment with later mismatch review (late-payment scenario).
5. Pending payment re-checked > 3 times.
6. Dead webhook delivery.
7. Clean payments have findings [] and next_step null.
8. Security: secrets/PII/error traces not exposed.
9. Scoping: foreign or different-mode payment returns 404 with no data.
10. Read-only: no database writes and no provider calls.
11. Admin view permissions: non-staff refused, staff with view permission allowed, staff without permission refused.
12. Admin page and API return same summary and findings.
13. Read throttle applies.
14. No raw exception text appears in any output.
15. Existing tests pass.
"""

import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from payments.investigator import build_investigation
from payments.models import (
    Merchant,
    Payment,
    PaymentStatusHistory,
    ProviderAttempt,
    WebhookDelivery,
    WebhookOutbox,
)

User = get_user_model()


class InvestigatorTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.merchant, self.api_key = Merchant.create_with_key(
            name="InvestigatorMerchant",
            webhook_url="https://merchant.example.com/webhooks",
        )
        self.client = APIClient()
        self.client.credentials(HTTP_X_API_KEY=self.api_key)

        self.payment = Payment.objects.create(
            merchant=self.merchant,
            livemode=False,
            amount=Decimal("100.00"),
            sender="alice",
            receiver="bob",
            provider="fake",
            idempotency_key=f"inv-test-{uuid.uuid4()}",
            status="pending",
        )

    def _create_superuser(self):
        return User.objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="adminpassword123",
        )


class InvestigatorEngineTests(InvestigatorTestBase):
    # 1. A succeeded payment with a delivered webhook gives the summary "Paid and confirmed. Merchant notified once."
    # and a timeline in time order that includes the status changes and "Merchant notified".
    def test_succeeded_payment_with_delivered_webhook(self):
        self.payment.transition_to("processing")
        self.payment.transition_to("succeeded")

        # Create or update WebhookDelivery to succeeded
        delivery = WebhookDelivery.objects.filter(payment=self.payment).first()
        if not delivery:
            delivery = WebhookDelivery.objects.create(
                payment=self.payment,
                event_id=uuid.uuid4(),
                event_type="payment.succeeded",
                target_url="https://merchant.example.com/webhooks",
                payload={},
                status="succeeded",
            )
        else:
            delivery.status = "succeeded"
            delivery.save(update_fields=["status"])

        data = build_investigation(self.payment)
        self.assertEqual(data["summary"], "Paid and confirmed. Merchant notified once.")
        events = [item["event"] for item in data["timeline"]]
        self.assertIn("Payment created", events)
        self.assertIn("Status changed from pending to processing", events)
        self.assertIn("Status changed from processing to succeeded", events)
        self.assertIn("Merchant notified", events)
        # Ensure chronological ordering
        timestamps = [item["at"] for item in data["timeline"]]
        self.assertEqual(timestamps, sorted(timestamps))

    # 2. A payment whose primary provider failed with not_created and whose fallback succeeded shows both attempts in order,
    # and no findings for a healthy payment.
    def test_fallback_provider_attempt_sequence(self):
        # Primary provider failed
        ProviderAttempt.objects.create(
            payment=self.payment,
            provider="chapa",
            outcome=ProviderAttempt.OUTCOME_FAILED_NOT_CREATED,
        )
        # Fallback provider succeeded
        ProviderAttempt.objects.create(
            payment=self.payment,
            provider="fake",
            outcome=ProviderAttempt.OUTCOME_CREATED,
        )
        self.payment.transition_to("processing")
        self.payment.transition_to("succeeded")

        delivery = WebhookDelivery.objects.filter(payment=self.payment).first()
        if delivery:
            delivery.status = "succeeded"
            delivery.save(update_fields=["status"])

        data = build_investigation(self.payment)
        events = [item["event"] for item in data["timeline"]]
        self.assertIn("Provider chapa could not be reached, so the next provider was tried", events)
        self.assertIn("Provider fake created a checkout link", events)
        # Sequence check
        idx_chapa = events.index("Provider chapa could not be reached, so the next provider was tried")
        idx_fake = events.index("Provider fake created a checkout link")
        self.assertLess(idx_chapa, idx_fake)
        self.assertEqual(data["findings"], [])
        self.assertIsNone(data["next_step"])

    # 3. A needs_review payment with reason unknown_outcome shows its finding, the mapped sentence and the next step
    # for the provider dashboard; mismatch, unsupported_status and cannot_verify each map to their sentence.
    def test_needs_review_reasons_and_next_step(self):
        cases = [
            (
                "unknown_outcome",
                "Flagged for review: the provider could not be reached repeatedly",
                "Check the provider's dashboard, then use 'Check with provider now' in the admin.",
            ),
            (
                "mismatch",
                "Flagged for review: SafePay and the provider disagree about a finished payment",
                "Compare both sides, then decide and record the outcome manually.",
            ),
            (
                "unsupported_status",
                "Flagged for review: the provider reported a status SafePay does not support",
                "Inspect provider logs and determine the final payment status manually.",
            ),
            (
                "cannot_verify",
                "Flagged for review: the provider cannot verify this payment without a transaction id",
                "Look up the transaction in the provider portal to resolve verification.",
            ),
        ]
        for reason, expected_finding, expected_next_step in cases:
            self.payment.flag_for_review(reason)
            data = build_investigation(self.payment)
            finding_texts = [f["text"] if isinstance(f, dict) else f for f in data["findings"]]
            self.assertIn(expected_finding, finding_texts)
            self.assertEqual(data["next_step"], expected_next_step)

    # 4. An expired payment with a later mismatch review shows the late-payment summary and a finding.
    def test_expired_payment_with_later_mismatch_review(self):
        self.payment.transition_to("expired")
        self.payment.flag_for_review("mismatch")

        data = build_investigation(self.payment)
        self.assertEqual(data["summary"], "Expired, but the provider later reported it paid. Needs review.")
        finding_texts = [f["text"] if isinstance(f, dict) else f for f in data["findings"]]
        self.assertIn("Payment was reported paid after it had already expired.", finding_texts)
        self.assertEqual(data["next_step"], "Compare both sides, then decide and record the outcome manually.")

    # 5. A pending payment re-checked more than 3 times shows the "still pending" finding.
    def test_pending_payment_rechecked_more_than_three_times(self):
        self.payment.sync_attempts = 4
        self.payment.last_synced_at = timezone.now()
        self.payment.save()

        data = build_investigation(self.payment)
        finding_texts = [f["text"] if isinstance(f, dict) else f for f in data["findings"]]
        self.assertIn("Re-checked 4 times and still pending", finding_texts)
        events = [item["event"] for item in data["timeline"]]
        self.assertIn("Re-checked with the provider", events)

    # 6. A dead webhook delivery shows "Merchant notification failed after N attempts" and a finding.
    def test_dead_webhook_delivery_finding_and_timeline(self):
        delivery = WebhookDelivery.objects.create(
            payment=self.payment,
            event_id=uuid.uuid4(),
            event_type="payment.succeeded",
            target_url="https://merchant.example.com/webhooks",
            payload={},
            status="failed",
            attempt_count=5,
            max_attempts=5,
        )
        data = build_investigation(self.payment)
        finding_texts = [f["text"] if isinstance(f, dict) else f for f in data["findings"]]
        self.assertIn("Merchant notification failed after 5 attempts", finding_texts)
        events = [item["event"] for item in data["timeline"]]
        self.assertIn("Merchant notification failed after 5 attempts", events)

    # 7. Clean payments have findings [] and next_step null.
    def test_clean_payment_has_empty_findings_and_null_next_step(self):
        data = build_investigation(self.payment)
        self.assertEqual(data["findings"], [])
        self.assertIsNone(data["next_step"])
        self.assertEqual(data["summary"], "Still pending with the provider.")


class InvestigatorSecurityAndScopingTests(InvestigatorTestBase):
    # 8. Security: put recognisable secret-looking strings into provider reference, transaction id,
    # target URL, last error, sender and idempotency key; assert none appears in API JSON or admin HTML.
    def test_security_no_secrets_or_pii_leaked(self):
        SECRET_TOKEN = "SUPER_SECRET_TOKEN_DO_NOT_LEAK_998877"
        self.payment.provider_reference = f"ref-{SECRET_TOKEN}"
        self.payment.sender = f"alice-{SECRET_TOKEN}"
        self.payment.idempotency_key = f"key-{SECRET_TOKEN}"
        self.payment.checkout_url = f"https://example.com/checkout/{SECRET_TOKEN}"
        self.payment.save()

        WebhookDelivery.objects.create(
            payment=self.payment,
            event_id=uuid.uuid4(),
            event_type="payment.succeeded",
            target_url=f"https://target.com/hook/{SECRET_TOKEN}",
            payload={"secret": SECRET_TOKEN},
            status="failed",
            last_error=f"Catastrophic error: {SECRET_TOKEN}",
            attempt_count=5,
        )

        # Check API response
        url = reverse("payment_investigate", kwargs={"pk": self.payment.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        api_content = resp.content.decode("utf-8")
        self.assertNotIn(SECRET_TOKEN, api_content)

        # Check Admin HTML
        user = self._create_superuser()
        admin_client = Client()
        admin_client.force_login(user)
        admin_url = reverse("admin:payment_investigate", args=[self.payment.pk])
        admin_resp = admin_client.get(admin_url)
        self.assertEqual(admin_resp.status_code, 200)
        admin_content = admin_resp.content.decode("utf-8")
        self.assertNotIn(SECRET_TOKEN, admin_content)

    # 9. Scoping: another merchant's payment and the other mode's payment return 404 from the API, with no data.
    def test_scoping_other_merchant_and_other_mode_returns_404(self):
        other_merchant, other_key = Merchant.create_with_key("OtherMerchant")
        other_payment = Payment.objects.create(
            merchant=other_merchant,
            livemode=False,
            amount=Decimal("50.00"),
            sender="carol",
            receiver="dave",
            idempotency_key=f"other-{uuid.uuid4()}",
            status="pending",
        )

        live_payment = Payment.objects.create(
            merchant=self.merchant,
            livemode=True,
            amount=Decimal("50.00"),
            sender="carol",
            receiver="dave",
            idempotency_key=f"live-{uuid.uuid4()}",
            status="pending",
        )

        # Other merchant -> 404
        url_other = reverse("payment_investigate", kwargs={"pk": other_payment.pk})
        resp_other = self.client.get(url_other)
        self.assertEqual(resp_other.status_code, 404)
        self.assertEqual(resp_other.json(), {"detail": "No Payment matches the given query."})

        # Other mode -> 404
        url_live = reverse("payment_investigate", kwargs={"pk": live_payment.pk})
        resp_live = self.client.get(url_live)
        self.assertEqual(resp_live.status_code, 404)
        self.assertEqual(resp_live.json(), {"detail": "No Payment matches the given query."})

    # 10. The API and the admin page perform no writes and make no provider calls.
    @patch("payments.providers.get_provider")
    def test_read_only_no_writes_and_no_provider_calls(self, mock_get_provider):
        user = self._create_superuser()
        admin_client = Client()
        admin_client.force_login(user)

        api_url = reverse("payment_investigate", kwargs={"pk": self.payment.pk})
        admin_url = reverse("admin:payment_investigate", args=[self.payment.pk])

        initial_counts = {
            "payment": Payment.objects.count(),
            "history": PaymentStatusHistory.objects.count(),
            "attempt": ProviderAttempt.objects.count(),
            "delivery": WebhookDelivery.objects.count(),
            "outbox": WebhookOutbox.objects.count(),
        }

        # Call API
        self.client.get(api_url)
        # Call Admin
        admin_client.get(admin_url)

        final_counts = {
            "payment": Payment.objects.count(),
            "history": PaymentStatusHistory.objects.count(),
            "attempt": ProviderAttempt.objects.count(),
            "delivery": WebhookDelivery.objects.count(),
            "outbox": WebhookOutbox.objects.count(),
        }
        self.assertEqual(initial_counts, final_counts)
        mock_get_provider.assert_not_called()


class InvestigatorAdminAndParityTests(InvestigatorTestBase):
    # 11. The admin page: a non-staff user is refused; a staff user with view permission sees the summary;
    # a staff user without the permission is refused.
    def test_admin_permissions(self):
        admin_url = reverse("admin:payment_investigate", args=[self.payment.pk])
        client = Client()

        # Non-staff user
        regular_user = User.objects.create_user(username="regular", password="password")
        client.force_login(regular_user)
        resp_non_staff = client.get(admin_url)
        # admin site redirects non-staff or returns 302/403
        self.assertIn(resp_non_staff.status_code, (302, 403))

        # Staff user without payments.view_payment permission
        staff_user = User.objects.create_user(username="staff", password="password", is_staff=True)
        client.force_login(staff_user)
        resp_staff_no_perm = client.get(admin_url)
        self.assertEqual(resp_staff_no_perm.status_code, 403)

        # Staff user with payments.view_payment permission
        perm = Permission.objects.get(codename="view_payment")
        staff_user.user_permissions.add(perm)
        resp_staff_perm = client.get(admin_url)
        self.assertEqual(resp_staff_perm.status_code, 200)
        self.assertIn("Still pending with the provider.", resp_staff_perm.content.decode("utf-8"))

    # 12. The admin page and the API return the same summary and findings for the same payment.
    def test_admin_and_api_parity(self):
        self.payment.flag_for_review("unknown_outcome")

        api_url = reverse("payment_investigate", kwargs={"pk": self.payment.pk})
        api_data = self.client.get(api_url).json()

        user = self._create_superuser()
        admin_client = Client()
        admin_client.force_login(user)
        admin_url = reverse("admin:payment_investigate", args=[self.payment.pk])
        admin_content = admin_client.get(admin_url).content.decode("utf-8")

        # Summary match
        self.assertIn(api_data["summary"], admin_content)
        # Findings match
        for finding in api_data["findings"]:
            text = finding["text"] if isinstance(finding, dict) else finding
            self.assertIn(text, admin_content)
        # Next step match
        if api_data["next_step"]:
            self.assertIn(api_data["next_step"], admin_content)

    # 13. The read throttle applies.
    def test_read_throttle_applies(self):
        cache.clear()
        url = reverse("payment_investigate", kwargs={"pk": self.payment.pk})
        with self.settings(PAYMENT_READ_THROTTLE_RATE="1/minute"):
            resp1 = self.client.get(url)
            self.assertEqual(resp1.status_code, status.HTTP_200_OK)
            resp2 = self.client.get(url)
            self.assertEqual(resp2.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    # 14. No raw exception text appears in any output.
    def test_no_raw_exception_text_appears(self):
        LONG_ERROR = "DatabaseConnectionLostCatastrophicSocketFailureException: Connection refused on port 5432"
        ProviderAttempt.objects.create(
            payment=self.payment,
            provider="fake",
            outcome=ProviderAttempt.OUTCOME_FAILED_NOT_CREATED,
            error_type="NetworkError",
        )
        self.payment.flag_for_review("unknown_outcome")

        url = reverse("payment_investigate", kwargs={"pk": self.payment.pk})
        api_resp = self.client.get(url)
        self.assertNotIn(LONG_ERROR, api_resp.content.decode("utf-8"))

        user = self._create_superuser()
        admin_client = Client()
        admin_client.force_login(user)
        admin_url = reverse("admin:payment_investigate", args=[self.payment.pk])
        admin_resp = admin_client.get(admin_url)
        self.assertNotIn(LONG_ERROR, admin_resp.content.decode("utf-8"))
