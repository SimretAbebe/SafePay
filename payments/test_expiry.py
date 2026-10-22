from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from payments.models import Merchant, Payment, PaymentStatusHistory, WebhookDelivery
from payments.providers import FakeProvider, ProviderError
from payments.services import expire_one, reconcile_one, sync_payment_from_provider
from payments.tasks import expire_pending_payments
from payments.test_utils import create_test_merchant


@override_settings(
    ENABLE_FAKE_PROVIDER=True,
    DEFAULT_PROVIDER="fake",
    PAYMENT_EXPIRY_SECONDS=86400,
    PAYMENT_EXPIRY_MIN_SECONDS=900,
    PAYMENT_EXPIRY_MAX_SECONDS=604800,
    EXPIRY_GRACE_SECONDS=900,
    EXPIRY_SWEEP_SECONDS=60,
    EXPIRY_BATCH_SIZE=50,
)
class PaymentExpiryTests(TransactionTestCase):

    def setUp(self):
        cache.clear()
        FakeProvider.reset()
        self.client = APIClient()
        self.merchant, self.api_key = create_test_merchant(
            "Expiry Test Merchant",
            webhook_url="https://merchant.example.com/webhook",
        )
        self.client.credentials(HTTP_X_API_KEY=self.api_key)
        self.now = timezone.now()

    def tearDown(self):
        cache.clear()
        FakeProvider.reset()

    # 1. A new checkout payment gets expires_at = now + the default;
    #    a valid expires_in_minutes is respected;
    #    values below the minimum, above the maximum, zero, negative and non-integer -> 400
    #    and no payment row is created; repeating the request with the same Idempotency-Key
    #    leaves expires_at unchanged.
    def test_checkout_session_expiry_configuration_and_validation(self):
        with patch("django.utils.timezone.now", return_value=self.now):
            # 1a. Default expires_in_minutes (omitted) -> expires_at = now + 86400
            resp_default = self.client.post(
                reverse("create_checkout_session"),
                {"amount": "100.00"},
                format="json",
                HTTP_IDEMPOTENCY_KEY="idem-exp-default",
            )
            self.assertEqual(resp_default.status_code, status.HTTP_201_CREATED)
            expected_default_iso = (self.now + timedelta(seconds=86400)).isoformat()
            self.assertEqual(resp_default.data["expires_at"], expected_default_iso)
            pay_default = Payment.objects.get(id=resp_default.data["id"])
            self.assertEqual(pay_default.expires_at, self.now + timedelta(seconds=86400))

            # Idempotent repeated request leaves expires_at unchanged
            resp_default_repeat = self.client.post(
                reverse("create_checkout_session"),
                {"amount": "100.00"},
                format="json",
                HTTP_IDEMPOTENCY_KEY="idem-exp-default",
            )
            self.assertEqual(resp_default_repeat.status_code, status.HTTP_200_OK)
            self.assertEqual(resp_default_repeat.data["expires_at"], expected_default_iso)

            # 1b. Valid expires_in_minutes (30 minutes = 1800s, between 900s and 604800s)
            resp_custom = self.client.post(
                reverse("create_checkout_session"),
                {"amount": "150.00", "expires_in_minutes": 30},
                format="json",
                HTTP_IDEMPOTENCY_KEY="idem-exp-custom",
            )
            self.assertEqual(resp_custom.status_code, status.HTTP_201_CREATED)
            expected_custom_iso = (self.now + timedelta(minutes=30)).isoformat()
            self.assertEqual(resp_custom.data["expires_at"], expected_custom_iso)
            pay_custom = Payment.objects.get(id=resp_custom.data["id"])
            self.assertEqual(pay_custom.expires_at, self.now + timedelta(minutes=30))

            # 1c. Invalid values: below minimum (14 minutes = 840s < 900s)
            initial_count = Payment.objects.count()
            resp_below = self.client.post(
                reverse("create_checkout_session"),
                {"amount": "100.00", "expires_in_minutes": 14},
                format="json",
                HTTP_IDEMPOTENCY_KEY="idem-exp-below",
            )
            self.assertEqual(resp_below.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(Payment.objects.count(), initial_count)

            # 1d. Invalid values: above maximum (10081 minutes = 604860s > 604800s)
            resp_above = self.client.post(
                reverse("create_checkout_session"),
                {"amount": "100.00", "expires_in_minutes": 10081},
                format="json",
                HTTP_IDEMPOTENCY_KEY="idem-exp-above",
            )
            self.assertEqual(resp_above.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(Payment.objects.count(), initial_count)

            # 1e. Zero
            resp_zero = self.client.post(
                reverse("create_checkout_session"),
                {"amount": "100.00", "expires_in_minutes": 0},
                format="json",
                HTTP_IDEMPOTENCY_KEY="idem-exp-zero",
            )
            self.assertEqual(resp_zero.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(Payment.objects.count(), initial_count)

            # 1f. Negative
            resp_neg = self.client.post(
                reverse("create_checkout_session"),
                {"amount": "100.00", "expires_in_minutes": -15},
                format="json",
                HTTP_IDEMPOTENCY_KEY="idem-exp-neg",
            )
            self.assertEqual(resp_neg.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(Payment.objects.count(), initial_count)

            # 1g. Non-integer: string, float, boolean, null
            for bad_val in ["30", 15.5, True, False, "abc"]:
                resp_non_int = self.client.post(
                    reverse("create_checkout_session"),
                    {"amount": "100.00", "expires_in_minutes": bad_val},
                    format="json",
                    HTTP_IDEMPOTENCY_KEY=f"idem-exp-bad-{bad_val}",
                )
                self.assertEqual(
                    resp_non_int.status_code,
                    status.HTTP_400_BAD_REQUEST,
                    f"Expected 400 for expires_in_minutes={bad_val!r}",
                )
                self.assertEqual(Payment.objects.count(), initial_count)

    # 2. Past expires_at + grace and the provider says pending:
    #    the payment becomes expired, exactly one history row (pending -> expired),
    #    exactly one WebhookDelivery with event type payment.expired (for a merchant with a webhook URL),
    #    next_sync_at None.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_past_expires_at_plus_grace_expires_payment(self, mock_webhook_delay):
        ref = "fake_ref_exp_2"
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="exp-test-2",
            amount=Decimal("120.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference=ref,
            status="pending",
            expires_at=self.now - timedelta(seconds=900 + 60),  # past expires_at + grace
            next_sync_at=self.now,
        )
        FakeProvider.simulate(ref, "pending")

        outcome = expire_one(payment.id, now=self.now)
        self.assertEqual(outcome, "expired")

        payment.refresh_from_db()
        self.assertEqual(payment.status, "expired")
        self.assertIsNone(payment.next_sync_at)

        # Exactly one history row: pending -> expired
        history = list(payment.history.values_list("from_status", "to_status"))
        self.assertEqual(history, [("pending", "expired")])

        # Exactly one WebhookDelivery with event_type payment.expired
        webhooks = WebhookDelivery.objects.filter(payment=payment)
        self.assertEqual(webhooks.count(), 1)
        delivery = webhooks.first()
        self.assertEqual(delivery.event_type, "payment.expired")
        self.assertEqual(delivery.payload["type"], "payment.expired")
        self.assertEqual(delivery.payload["data"]["status"], "expired")

    # 3. Before expires_at, and between expires_at and expires_at + grace: nothing happens.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_before_expiry_or_inside_grace_skips(self, mock_webhook_delay):
        # 3a. Before expires_at (expires_at in future)
        p_future = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="exp-future-3",
            amount=Decimal("100.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference="fake_future_3",
            status="pending",
            expires_at=self.now + timedelta(seconds=300),
            next_sync_at=self.now,
        )
        FakeProvider.simulate("fake_future_3", "pending")
        outcome_future = expire_one(p_future.id, now=self.now)
        self.assertEqual(outcome_future, "skipped")
        p_future.refresh_from_db()
        self.assertEqual(p_future.status, "pending")
        self.assertEqual(p_future.history.count(), 0)
        self.assertEqual(WebhookDelivery.objects.filter(payment=p_future).count(), 0)

        # 3b. Between expires_at and expires_at + grace (expired 300s ago, grace is 900s)
        p_grace = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="exp-grace-3",
            amount=Decimal("100.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference="fake_grace_3",
            status="pending",
            expires_at=self.now - timedelta(seconds=300),
            next_sync_at=self.now,
        )
        FakeProvider.simulate("fake_grace_3", "pending")
        outcome_grace = expire_one(p_grace.id, now=self.now)
        self.assertEqual(outcome_grace, "skipped")
        p_grace.refresh_from_db()
        self.assertEqual(p_grace.status, "pending")
        self.assertEqual(p_grace.history.count(), 0)
        self.assertEqual(WebhookDelivery.objects.filter(payment=p_grace).count(), 0)

    # 4. The provider says PAID at expiry time:
    #    the payment ends succeeded via the sync (two history rows through processing),
    #    NOT expired, with one succeeded webhook.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_provider_paid_at_expiry_time_settles_succeeded(self, mock_webhook_delay):
        ref = "fake_paid_at_expiry_4"
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="exp-paid-4",
            amount=Decimal("200.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference=ref,
            status="pending",
            expires_at=self.now - timedelta(seconds=1000),
        )
        FakeProvider.simulate(ref, "paid")

        outcome = expire_one(payment.id, now=self.now)
        self.assertEqual(outcome, "settled")

        payment.refresh_from_db()
        self.assertEqual(payment.status, "succeeded")

        # Two history rows through processing
        history = list(payment.history.values_list("from_status", "to_status"))
        self.assertEqual(
            history,
            [("pending", "processing"), ("processing", "succeeded")],
        )

        # Exactly one webhook for payment.succeeded, NOT payment.expired
        deliveries = WebhookDelivery.objects.filter(payment=payment)
        self.assertEqual(deliveries.count(), 1)
        self.assertEqual(deliveries.first().event_type, "payment.succeeded")

    # 5. ProviderError (test double) at expiry time:
    #    the payment stays pending, no history, no webhook, not expired;
    #    a later sweep with a working provider expires it.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_provider_error_at_expiry_time_stays_pending_then_expires_later(self, mock_webhook_delay):
        ref = "fake_err_5"
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="exp-err-5",
            amount=Decimal("75.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference=ref,
            status="pending",
            expires_at=self.now - timedelta(seconds=1000),
        )

        # Inject provider error on verify
        with patch.object(FakeProvider, "verify", side_effect=ProviderError("Network timeout")):
            outcome = expire_one(payment.id, now=self.now)
            self.assertEqual(outcome, "unknown")

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertEqual(payment.history.count(), 0)
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 0)

        # Later sweep with working provider
        FakeProvider.simulate(ref, "pending")
        later_now = self.now + timedelta(seconds=60)
        later_outcome = expire_one(payment.id, now=later_now)
        self.assertEqual(later_outcome, "expired")

        payment.refresh_from_db()
        self.assertEqual(payment.status, "expired")
        self.assertEqual(payment.history.count(), 1)
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 1)

    # 6. A payment without a provider expires without calling any provider.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_payment_without_provider_expires(self, mock_webhook_delay):
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="exp-noprov-6",
            amount=Decimal("50.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider=None,
            provider_reference=None,
            status="pending",
            expires_at=self.now - timedelta(seconds=1000),
        )

        with patch.object(FakeProvider, "verify") as mock_verify:
            outcome = expire_one(payment.id, now=self.now)
            self.assertEqual(outcome, "expired")
            mock_verify.assert_not_called()

        payment.refresh_from_db()
        self.assertEqual(payment.status, "expired")
        self.assertEqual(payment.history.count(), 1)
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 1)

    # 7. Legacy payments with expires_at NULL are never expired.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_legacy_payments_with_null_expires_at_never_expire(self, mock_webhook_delay):
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="exp-legacy-7",
            amount=Decimal("50.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference="fake_legacy_7",
            status="pending",
            expires_at=None,
        )
        FakeProvider.simulate("fake_legacy_7", "pending")

        outcome = expire_one(payment.id, now=self.now + timedelta(days=365))
        self.assertEqual(outcome, "skipped")

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertEqual(payment.history.count(), 0)
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 0)

    # 8. Late payment: after a payment is expired, the provider later reports paid
    #    (via sync_payment_from_provider or the inbound webhook path):
    #    status stays expired, needs_review becomes True with reason "mismatch",
    #    an alert is scheduled, no history row, no new webhook.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    @patch("payments.alerts.schedule_review_alert")
    def test_late_payment_after_expiry_flags_mismatch_and_schedules_alert(
        self, mock_schedule_alert, mock_webhook_delay
    ):
        ref = "fake_late_8"
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="exp-late-8",
            amount=Decimal("300.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference=ref,
            status="pending",
            expires_at=self.now - timedelta(seconds=1000),
        )
        FakeProvider.simulate(ref, "pending")

        # Expire payment
        expire_one(payment.id, now=self.now)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "expired")
        initial_history_count = payment.history.count()
        initial_webhook_count = WebhookDelivery.objects.filter(payment=payment).count()
        self.assertEqual(initial_history_count, 1)
        self.assertEqual(initial_webhook_count, 1)

        # Later, customer pays at provider
        FakeProvider.simulate(ref, "paid")

        # Sync from provider
        sync_result = sync_payment_from_provider(payment.id)
        self.assertEqual(sync_result.outcome, "mismatch")

        payment.refresh_from_db()
        self.assertEqual(payment.status, "expired")
        self.assertTrue(payment.needs_review)
        self.assertEqual(payment.review_reason, "mismatch")
        self.assertIsNotNone(payment.needs_review_at)

        # No new history row, no new webhook
        self.assertEqual(payment.history.count(), initial_history_count)
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), initial_webhook_count)
        mock_schedule_alert.assert_called()

    # 9. Running the sweep twice creates no second history row and no second webhook;
    #    payments with needs_review True are skipped.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_idempotent_sweep_and_skipping_needs_review(self, mock_webhook_delay):
        ref = "fake_sweep_9"
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="exp-sweep-9",
            amount=Decimal("100.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference=ref,
            status="pending",
            expires_at=self.now - timedelta(seconds=1000),
        )
        FakeProvider.simulate(ref, "pending")

        # Sweep 1
        with patch("django.utils.timezone.now", return_value=self.now):
            counts1 = expire_pending_payments()
            self.assertEqual(counts1["expired"], 1)

        payment.refresh_from_db()
        self.assertEqual(payment.status, "expired")
        self.assertEqual(payment.history.count(), 1)
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 1)

        # Sweep 2
        with patch("django.utils.timezone.now", return_value=self.now + timedelta(seconds=60)):
            counts2 = expire_pending_payments()
            self.assertEqual(counts2["expired"], 0)

        payment.refresh_from_db()
        self.assertEqual(payment.history.count(), 1)
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 1)

        # Payments with needs_review=True are skipped
        p_review = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="exp-review-9",
            amount=Decimal("100.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference="fake_review_9",
            status="pending",
            expires_at=self.now - timedelta(seconds=1000),
            needs_review=True,
            review_reason="unknown_outcome",
        )
        FakeProvider.simulate("fake_review_9", "pending")
        outcome_review = expire_one(p_review.id, now=self.now)
        self.assertEqual(outcome_review, "skipped")
        p_review.refresh_from_db()
        self.assertEqual(p_review.status, "pending")

    # 10. cancel_payment on an expired payment -> 409;
    #     verify_payment returns status "expired";
    #     the list endpoint with status=expired returns it;
    #     the checkout endpoint with the same Idempotency-Key returns 200 with status "expired"
    #     and checkout_url null.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_api_endpoints_behavior_with_expired_payment(self, mock_webhook_delay):
        ref = "fake_endpoints_10"
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="exp-endpoint-10",
            amount=Decimal("150.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference=ref,
            status="pending",
            checkout_url="https://fake.local/pay/10",
            expires_at=self.now - timedelta(seconds=1000),
        )
        FakeProvider.simulate(ref, "pending")
        expire_one(payment.id, now=self.now)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "expired")

        # 10a. cancel_payment -> 409 Conflict
        cancel_url = reverse("cancel_payment", kwargs={"pk": payment.pk})
        resp_cancel = self.client.post(cancel_url)
        self.assertEqual(resp_cancel.status_code, status.HTTP_409_CONFLICT)

        # 10b. verify_payment returns status "expired" with minimal body
        verify_url = reverse("verify_payment", kwargs={"pk": payment.pk})
        resp_verify = self.client.get(verify_url)
        self.assertEqual(resp_verify.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_verify.data["status"], "expired")
        self.assertEqual(set(resp_verify.data.keys()), {"id", "status", "updated_at"})

        # 10c. list endpoint with status=expired returns it
        list_url = reverse("payment_list")
        resp_list = self.client.get(f"{list_url}?status=expired")
        self.assertEqual(resp_list.status_code, status.HTTP_200_OK)
        results = resp_list.data["results"]
        matched = [r for r in results if r["id"] == payment.id]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["status"], "expired")

        # 10d. checkout endpoint with same Idempotency-Key returns 200 with status "expired" and checkout_url null
        resp_checkout = self.client.post(
            reverse("create_checkout_session"),
            {"amount": "150.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="exp-endpoint-10",
        )
        self.assertEqual(resp_checkout.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_checkout.data["status"], "expired")
        self.assertIsNone(resp_checkout.data["checkout_url"])
        self.assertIsNotNone(resp_checkout.data["expires_at"])

        # payment_detail returns checkout_url as null and includes expires_at
        detail_url = reverse("payment_detail", kwargs={"pk": payment.pk})
        resp_detail = self.client.get(detail_url)
        self.assertEqual(resp_detail.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_detail.data["status"], "expired")
        self.assertIsNone(resp_detail.data["checkout_url"])
        self.assertIsNotNone(resp_detail.data["expires_at"])

    # 11. Reconciliation skips expired payments and clears their next_sync_at.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_reconciliation_skips_expired_payments_and_clears_next_sync(self, mock_webhook_delay):
        ref = "fake_rec_skip_11"
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="exp-rec-11",
            amount=Decimal("100.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference=ref,
            status="expired",
            next_sync_at=self.now,
        )
        FakeProvider.simulate(ref, "pending")

        result = reconcile_one(payment.id, now=self.now)
        self.assertEqual(result.outcome, "skipped")

        payment.refresh_from_db()
        self.assertEqual(payment.status, "expired")
        self.assertIsNone(payment.next_sync_at)

    # 12. The webhook payload type is payment.expired and carries the right livemode flag;
    #     a merchant without a webhook URL gets none.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_webhook_payload_and_merchant_without_webhook_url(self, mock_webhook_delay):
        # 12a. Livemode flag carried correctly in webhook payload
        p_live = Payment.objects.create(
            merchant=self.merchant,
            livemode=True,
            idempotency_key="exp-live-12",
            amount=Decimal("100.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference="fake_live_12",
            status="pending",
            expires_at=self.now - timedelta(seconds=1000),
        )
        self.merchant.live_webhook_url = "https://merchant.example.com/live-webhook"
        self.merchant.save(update_fields=["live_webhook_url"])
        FakeProvider.simulate("fake_live_12", "pending")

        expire_one(p_live.id, now=self.now)
        deliv_live = WebhookDelivery.objects.filter(payment=p_live).first()
        self.assertIsNotNone(deliv_live)
        self.assertEqual(deliv_live.event_type, "payment.expired")
        self.assertEqual(deliv_live.payload["type"], "payment.expired")
        self.assertTrue(deliv_live.payload["livemode"])
        self.assertEqual(deliv_live.target_url, "https://merchant.example.com/live-webhook")

        # 12b. Merchant without a webhook URL gets NO webhook delivery
        merchant_no_url, _ = create_test_merchant("No URL Merchant", webhook_url="")
        p_no_url = Payment.objects.create(
            merchant=merchant_no_url,
            livemode=False,
            idempotency_key="exp-nourl-12",
            amount=Decimal("100.00"),
            sender="bob",
            receiver=merchant_no_url.name,
            provider="fake",
            provider_reference="fake_nourl_12",
            status="pending",
            expires_at=self.now - timedelta(seconds=1000),
        )
        FakeProvider.simulate("fake_nourl_12", "pending")
        expire_one(p_no_url.id, now=self.now)
        p_no_url.refresh_from_db()
        self.assertEqual(p_no_url.status, "expired")
        self.assertEqual(WebhookDelivery.objects.filter(payment=p_no_url).count(), 0)

    # 13. The sweep respects EXPIRY_BATCH_SIZE and never raises when one payment errors.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_sweep_batch_size_and_error_resilience(self, mock_webhook_delay):
        # Create 4 payments due for expiry
        p_ids = []
        for i in range(4):
            ref = f"fake_batch_13_{i}"
            p = Payment.objects.create(
                merchant=self.merchant,
                idempotency_key=f"exp-batch-13-{i}",
                amount=Decimal("50.00"),
                sender="alice",
                receiver=self.merchant.name,
                provider="fake",
                provider_reference=ref,
                status="pending",
                expires_at=self.now - timedelta(seconds=1000 + i * 10),
            )
            FakeProvider.simulate(ref, "pending")
            p_ids.append(p.id)

        # Batch size 2: only 2 should be processed
        with override_settings(EXPIRY_BATCH_SIZE=2):
            with patch("django.utils.timezone.now", return_value=self.now):
                counts = expire_pending_payments()
                self.assertEqual(counts["expired"], 2)

        expired_count = Payment.objects.filter(id__in=p_ids, status="expired").count()
        pending_count = Payment.objects.filter(id__in=p_ids, status="pending").count()
        self.assertEqual(expired_count, 2)
        self.assertEqual(pending_count, 2)

        # Sweep never raises when one payment errors
        err_id = p_ids[0]
        real_expire_one = expire_one

        def flaky_expire_one(pid, now=None):
            if pid == err_id:
                raise RuntimeError("DB connection dropped")
            return real_expire_one(pid, now=now)

        with override_settings(EXPIRY_BATCH_SIZE=10):
            with patch("django.utils.timezone.now", return_value=self.now):
                with patch("payments.services.expire_one", side_effect=flaky_expire_one):
                    # Must NOT raise exception
                    counts_err = expire_pending_payments()
                    self.assertEqual(counts_err["unknown"], 1)
                    self.assertEqual(counts_err["expired"], 1)
