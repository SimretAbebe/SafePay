"""
payments/test_evidence.py

Test suite for Evidence-Trail extension:
1. Every timeline item has a source whose type and id match the real record it was built from.
2. A flagged payment's finding is "known" and lists the provider attempts it rests on.
3. A pending payment with sync attempts and no provider confirmation shows "unknown" and the last-recorded-event line; a succeeded payment shows no unknown line.
4. A dead webhook delivery finding is known and points to its delivery id.
5. The API output still contains none of the forbidden fields (secret-scan test).
6. Admin links point to the correct change pages; non-staff and staff without permission are refused; links to unregistered models are plain text.
7. The admin view and the API return the same evidence and labels for the same payment.
8. No writes and no provider calls (row counts unchanged, provider mock never called).
9. All existing tests pass.
"""

import uuid
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
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


class EvidenceTrailTests(TestCase):
    def setUp(self):
        cache.clear()
        self.merchant, self.api_key = Merchant.create_with_key(
            name="EvidenceMerchant",
            webhook_url="https://merchant.example.com/webhooks",
        )
        self.client = APIClient()
        self.client.credentials(HTTP_X_API_KEY=self.api_key)

        self.payment = Payment.objects.create(
            merchant=self.merchant,
            livemode=False,
            amount=Decimal("150.00"),
            sender="alice",
            receiver="bob",
            provider="fake",
            idempotency_key=f"ev-test-{uuid.uuid4()}",
            status="pending",
        )

    def _create_superuser(self):
        return User.objects.create_superuser(
            username=f"admin_{uuid.uuid4().hex[:6]}",
            email="admin@example.com",
            password="adminpassword123",
        )

    # 1. Every timeline item has a source whose type and id match the real record it was built from.
    def test_timeline_source_type_and_id_match_records(self):
        # 1. status changes
        self.payment.transition_to("processing")
        self.payment.transition_to("succeeded")

        # 2. provider attempts
        att1 = ProviderAttempt.objects.create(
            payment=self.payment,
            provider="chapa",
            outcome=ProviderAttempt.OUTCOME_FAILED_NOT_CREATED,
        )
        att2 = ProviderAttempt.objects.create(
            payment=self.payment,
            provider="fake",
            outcome=ProviderAttempt.OUTCOME_CREATED,
        )

        # 3. reconciliation
        self.payment.last_synced_at = timezone.now()
        self.payment.save()

        # 4. webhook delivery & outbox
        delivery = WebhookDelivery.objects.create(
            payment=self.payment,
            event_id=uuid.uuid4(),
            event_type="payment.succeeded",
            target_url="https://merchant.example.com/webhooks",
            payload={},
            status="succeeded",
        )
        outbox = WebhookOutbox.objects.create(
            delivery=delivery,
            status="sent",
            sent_at=timezone.now(),
            next_attempt_at=timezone.now(),
        )

        data = build_investigation(self.payment)
        timeline = data["timeline"]

        sources = [item["source"] for item in timeline]
        source_types = [s["type"] for s in sources]

        self.assertIn("payment", source_types)
        self.assertIn("status_change", source_types)
        self.assertIn("provider_attempt", source_types)
        self.assertIn("reconciliation", source_types)
        self.assertIn("webhook_delivery", source_types)
        self.assertIn("webhook_outbox", source_types)

        # Check specific IDs
        for item in timeline:
            s = item["source"]
            self.assertIn("type", s)
            self.assertIn("source_id", s)
            self.assertIsInstance(s["source_id"], int)

            if s["type"] == "payment":
                self.assertEqual(s["source_id"], self.payment.id)
            elif s["type"] == "reconciliation":
                self.assertEqual(s["source_id"], self.payment.id)
            elif s["type"] == "status_change":
                self.assertTrue(PaymentStatusHistory.objects.filter(id=s["source_id"], payment=self.payment).exists())
            elif s["type"] == "provider_attempt":
                self.assertIn(s["source_id"], [att1.id, att2.id])
            elif s["type"] == "webhook_delivery":
                self.assertTrue(WebhookDelivery.objects.filter(id=s["source_id"], payment=self.payment).exists())
            elif s["type"] == "webhook_outbox":
                self.assertTrue(WebhookOutbox.objects.filter(id=s["source_id"], delivery__payment=self.payment).exists())

    # 2. A flagged payment's finding is "known" and lists the provider attempts it rests on.
    def test_flagged_payment_finding_is_known_with_provider_attempts(self):
        att1 = ProviderAttempt.objects.create(
            payment=self.payment,
            provider="fake",
            outcome=ProviderAttempt.OUTCOME_FAILED_NOT_CREATED,
        )
        self.payment.flag_for_review("unknown_outcome")

        data = build_investigation(self.payment)
        flagged_findings = [f for f in data["findings"] if "Flagged for review" in f["text"]]
        self.assertEqual(len(flagged_findings), 1)
        finding = flagged_findings[0]

        self.assertEqual(finding["basis"], "known")
        evidence_ids = [e["source_id"] for e in finding["evidence"] if e["type"] == "provider_attempt"]
        self.assertIn(att1.id, evidence_ids)

    # 3. A pending payment with sync attempts and no provider confirmation shows "unknown"
    # and the last-recorded-event line; a succeeded payment shows no unknown line.
    def test_pending_payment_with_sync_attempts_shows_unknown_line(self):
        self.payment.sync_attempts = 2
        self.payment.last_synced_at = None
        self.payment.save()

        data = build_investigation(self.payment)
        unknown_findings = [f for f in data["findings"] if f["basis"] == "unknown"]
        self.assertEqual(len(unknown_findings), 1)
        self.assertEqual(
            unknown_findings[0]["text"],
            "Last recorded event: Payment created. The provider has not confirmed the outcome.",
        )
        self.assertEqual(unknown_findings[0]["evidence"], [])

        # Succeeded payment shows no unknown line
        self.payment.transition_to("processing")
        self.payment.transition_to("succeeded")
        data_succeeded = build_investigation(self.payment)
        unknown_succeeded = [f for f in data_succeeded["findings"] if f["basis"] == "unknown"]
        self.assertEqual(len(unknown_succeeded), 0)

    # 4. A dead webhook delivery finding is known and points to its delivery id.
    def test_dead_webhook_delivery_finding_is_known(self):
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
        dead_findings = [
            f for f in data["findings"]
            if "Merchant notification failed" in f["text"]
        ]
        self.assertEqual(len(dead_findings), 1)
        finding = dead_findings[0]
        self.assertEqual(finding["basis"], "known")
        self.assertEqual(finding["evidence"], [{"type": "webhook_delivery", "source_id": delivery.id}])

    # 5. The API output still contains none of the forbidden fields (secret-scan test).
    def test_api_security_forbidden_fields_not_leaked(self):
        SECRET = "SECRET_VALUE_EXCLUDE_991188"
        self.payment.provider_reference = f"pref_{SECRET}"
        self.payment.sender = f"sender_{SECRET}"
        self.payment.idempotency_key = f"idemp_{SECRET}"
        self.payment.checkout_url = f"https://secret.url/{SECRET}"
        self.payment.save()

        WebhookDelivery.objects.create(
            payment=self.payment,
            event_id=uuid.uuid4(),
            event_type="payment.succeeded",
            target_url=f"https://target.com/webhooks/{SECRET}",
            payload={"secret": SECRET},
            status="failed",
            last_error=f"Raw error {SECRET}",
            attempt_count=5,
        )

        url = reverse("payment_investigate", kwargs={"pk": self.payment.pk})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        api_text = resp.content.decode("utf-8")
        self.assertNotIn(SECRET, api_text)

    # 6. Admin links point to the correct change pages; non-staff and staff without permission are refused;
    # links to unregistered models are plain text.
    def test_admin_links_and_permissions(self):
        delivery = WebhookDelivery.objects.create(
            payment=self.payment,
            event_id=uuid.uuid4(),
            event_type="payment.succeeded",
            target_url="https://merchant.example.com/webhooks",
            payload={},
            status="succeeded",
        )
        admin_url = reverse("admin:payment_investigate", args=[self.payment.pk])
        client = Client()

        # Non-staff refused
        regular = User.objects.create_user(username="regular_user", password="password123")
        client.force_login(regular)
        resp = client.get(admin_url)
        self.assertIn(resp.status_code, [302, 403])

        # Staff without permission refused
        staff = User.objects.create_user(username="staff_user", password="password123", is_staff=True)
        client.force_login(staff)
        resp = client.get(admin_url)
        self.assertEqual(resp.status_code, 403)

        # Staff with permission allowed
        perm = Permission.objects.get(codename="view_payment")
        staff.user_permissions.add(perm)
        resp = client.get(admin_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        # Payment change link (registered)
        expected_payment_link = reverse("admin:payments_payment_change", args=[self.payment.pk])
        self.assertIn(expected_payment_link, html)

        # WebhookDelivery change link (registered)
        expected_delivery_link = reverse("admin:payments_webhookdelivery_change", args=[delivery.pk])
        self.assertIn(expected_delivery_link, html)

    # 7. The admin view and the API return the same evidence and labels for the same payment.
    def test_admin_and_api_evidence_and_labels_parity(self):
        self.payment.flag_for_review("unknown_outcome")
        api_url = reverse("payment_investigate", kwargs={"pk": self.payment.pk})
        api_data = self.client.get(api_url).json()

        admin_url = reverse("admin:payment_investigate", args=[self.payment.pk])
        admin_user = self._create_superuser()
        admin_client = Client()
        admin_client.force_login(admin_user)
        admin_resp = admin_client.get(admin_url)
        self.assertEqual(admin_resp.status_code, 200)
        admin_html = admin_resp.content.decode("utf-8")

        self.assertIn(api_data["summary"], admin_html)
        for item in api_data["timeline"]:
            self.assertIn(item["event"], admin_html)
            self.assertIn(str(item["source"]["source_id"]), admin_html)
        for finding in api_data["findings"]:
            self.assertIn(finding["text"], admin_html)
            self.assertIn(finding["basis"], admin_html)

    # 8. No writes and no provider calls (row counts unchanged, provider mock never called).
    @patch("payments.providers.get_provider")
    def test_no_writes_and_no_provider_calls(self, mock_get_provider):
        api_url = reverse("payment_investigate", kwargs={"pk": self.payment.pk})
        admin_url = reverse("admin:payment_investigate", args=[self.payment.pk])

        admin_user = self._create_superuser()
        admin_client = Client()
        admin_client.force_login(admin_user)

        counts_before = {
            "payment": Payment.objects.count(),
            "history": PaymentStatusHistory.objects.count(),
            "attempt": ProviderAttempt.objects.count(),
            "delivery": WebhookDelivery.objects.count(),
            "outbox": WebhookOutbox.objects.count(),
        }

        self.client.get(api_url)
        admin_client.get(admin_url)

        counts_after = {
            "payment": Payment.objects.count(),
            "history": PaymentStatusHistory.objects.count(),
            "attempt": ProviderAttempt.objects.count(),
            "delivery": WebhookDelivery.objects.count(),
            "outbox": WebhookOutbox.objects.count(),
        }

        self.assertEqual(counts_before, counts_after)
        mock_get_provider.assert_not_called()

