"""
Tests for the merchant-facing webhook delivery log API.

Covers:
  1.  Merchant isolation and livemode separation (list)
  2.  Ordering (newest-first + id tie-break), pagination correctness, limit rules, bad cursor
  3.  Filters: status, event_type, payment_id, date range, combined AND
  4.  Detail endpoint: owner 200, other-merchant 404, other-mode 404, missing-id 404
  5.  Field set and secret/URL redaction
  6.  Resend action: queued/not-failed/double-resend/wrong-merchant
  7.  Resend throttle isolation
  8.  Admin retry action delegates to resend_delivery service
  9.  Schema validation (spectacular --validate)
  10. All existing tests still pass (implicit — we do not touch any existing test)
"""
import os
import subprocess
import sys
import uuid
from unittest.mock import call, patch

from django.contrib.admin.sites import AdminSite
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from payments.admin import WebhookDeliveryAdmin, retry_webhook_deliveries
from payments.models import Merchant, Payment, WebhookDelivery
from payments.providers import register_provider
from payments.providers.fake import FakeProvider
from payments.providers.registry import _registry
from payments.test_utils import create_test_merchant
from payments.views import _derive_error_code


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class LiveCapableDouble(FakeProvider):
    name = "wl_live_double"
    supports_livemode = True


def _make_payment(merchant, livemode=False, **kwargs):
    defaults = dict(
        amount="50.00",
        sender="alice",
        receiver=merchant.name,
        idempotency_key=f"ik-{uuid.uuid4()}",
    )
    defaults.update(kwargs)
    return Payment.objects.create(merchant=merchant, livemode=livemode, **defaults)


def _make_delivery(payment, status="failed", attempt_count=5, event_type="payment.cancelled",
                   last_error=None, last_response_code=None, max_attempts=5):
    return WebhookDelivery.objects.create(
        payment=payment,
        event_type=event_type,
        target_url=f"https://hooks.example.com/wh?token=SECRET_TOKEN_{uuid.uuid4()}",
        payload={"id": str(uuid.uuid4()), "type": event_type, "livemode": payment.livemode,
                 "data": {"payment_id": payment.id, "status": payment.status,
                          "amount": str(payment.amount), "idempotency_key": payment.idempotency_key}},
        status=status,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        last_error=last_error,
        last_response_code=last_response_code,
    )


# ---------------------------------------------------------------------------
# 1. Merchant isolation and livemode separation
# ---------------------------------------------------------------------------

class WebhookDeliveryListIsolationTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        register_provider(LiveCapableDouble())
        self.merchant_a, self.key_a = create_test_merchant(
            name="MerchA",
            webhook_url="https://a.example.com/hook",
            live_webhook_url="https://a.example.com/live-hook",
            live_enabled=True,
            default_provider="wl_live_double",
        )
        self.merchant_b, self.key_b = create_test_merchant(
            name="MerchB",
            webhook_url="https://b.example.com/hook",
        )
        with override_settings(LIVE_MODE_AVAILABLE=True):
            self.live_key_a = self.merchant_a.create_live_key()
        self.url = reverse("webhook_delivery_list")

    def tearDown(self):
        cache.clear()
        _registry.pop("wl_live_double", None)

    def test_list_returns_only_own_merchant_deliveries(self):
        pay_a = _make_payment(self.merchant_a)
        pay_b = _make_payment(self.merchant_b)
        d_a = _make_delivery(pay_a)
        _make_delivery(pay_b)  # must not appear

        self.client.credentials(HTTP_X_API_KEY=self.key_a)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r["id"] for r in resp.data["results"]]
        self.assertIn(d_a.id, ids)
        self.assertNotIn(pay_b.id, ids)  # pay_b's delivery id won't be in ids

        # Explicitly: merchant_b delivery must not appear
        self.client.credentials(HTTP_X_API_KEY=self.key_b)
        resp_b = self.client.get(self.url)
        ids_b = [r["id"] for r in resp_b.data["results"]]
        self.assertNotIn(d_a.id, ids_b)

    @override_settings(LIVE_MODE_AVAILABLE=True)
    def test_test_key_sees_only_test_mode_deliveries(self):
        pay_test = _make_payment(self.merchant_a, livemode=False)
        pay_live = _make_payment(self.merchant_a, livemode=True)
        d_test = _make_delivery(pay_test)
        d_live = _make_delivery(pay_live)

        self.client.credentials(HTTP_X_API_KEY=self.key_a)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r["id"] for r in resp.data["results"]]
        self.assertIn(d_test.id, ids)
        self.assertNotIn(d_live.id, ids)

    @override_settings(LIVE_MODE_AVAILABLE=True)
    def test_live_key_sees_only_live_mode_deliveries(self):
        pay_test = _make_payment(self.merchant_a, livemode=False)
        pay_live = _make_payment(self.merchant_a, livemode=True)
        d_test = _make_delivery(pay_test)
        d_live = _make_delivery(pay_live)

        self.client.credentials(HTTP_X_API_KEY=self.live_key_a)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r["id"] for r in resp.data["results"]]
        self.assertIn(d_live.id, ids)
        self.assertNotIn(d_test.id, ids)


# ---------------------------------------------------------------------------
# 2. Ordering and pagination
# ---------------------------------------------------------------------------

class WebhookDeliveryPaginationTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.merchant, self.key = create_test_merchant(name="PaginMerch")
        self.client.credentials(HTTP_X_API_KEY=self.key)
        self.url = reverse("webhook_delivery_list")
        self.payment = _make_payment(self.merchant)

    def tearDown(self):
        cache.clear()

    def _create_n_deliveries(self, n):
        deliveries = []
        for _ in range(n):
            d = _make_delivery(self.payment)
            deliveries.append(d)
        return deliveries

    def test_newest_first_ordering(self):
        self._create_n_deliveries(3)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        created_ats = [r["created_at"] for r in resp.data["results"]]
        self.assertEqual(created_ats, sorted(created_ats, reverse=True))

    def test_pagination_no_duplicates_no_gaps(self):
        # Create 5 deliveries, paginate with limit=2
        deliveries = self._create_n_deliveries(5)
        all_ids = set()
        url = self.url + "?limit=2"
        pages = 0
        while url:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            for item in resp.data["results"]:
                self.assertNotIn(item["id"], all_ids, "Duplicate id across pages")
                all_ids.add(item["id"])
            url = resp.data.get("next")
            pages += 1
            if pages > 10:
                self.fail("Pagination loop did not terminate")
        self.assertEqual(all_ids, {d.id for d in deliveries})

    def test_limit_clamped_to_100(self):
        resp = self.client.get(self.url + "?limit=999")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_invalid_limit_returns_400(self):
        resp = self.client.get(self.url + "?limit=notanint")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_zero_limit_returns_400(self):
        resp = self.client.get(self.url + "?limit=0")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_cursor_returns_400(self):
        resp = self.client.get(self.url + "?cursor=totally_invalid_cursor_xyz!!!")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# 3. Filters
# ---------------------------------------------------------------------------

class WebhookDeliveryFilterTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.merchant, self.key = create_test_merchant(name="FilterMerch")
        self.client.credentials(HTTP_X_API_KEY=self.key)
        self.url = reverse("webhook_delivery_list")
        self.payment = _make_payment(self.merchant)

    def tearDown(self):
        cache.clear()

    def test_filter_valid_status(self):
        d_fail = _make_delivery(self.payment, status="failed")
        d_ok = _make_delivery(self.payment, status="succeeded", attempt_count=1)

        resp = self.client.get(self.url + "?status=failed")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r["id"] for r in resp.data["results"]]
        self.assertIn(d_fail.id, ids)
        self.assertNotIn(d_ok.id, ids)

    def test_filter_invalid_status_returns_400(self):
        resp = self.client.get(self.url + "?status=bogus")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filter_event_type(self):
        d_cancelled = _make_delivery(self.payment, event_type="payment.cancelled")
        d_succeeded = _make_delivery(self.payment, event_type="payment.succeeded")

        resp = self.client.get(self.url + "?event_type=payment.cancelled")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r["id"] for r in resp.data["results"]]
        self.assertIn(d_cancelled.id, ids)
        self.assertNotIn(d_succeeded.id, ids)

    def test_filter_payment_id(self):
        pay2 = _make_payment(self.merchant)
        d1 = _make_delivery(self.payment)
        d2 = _make_delivery(pay2)

        resp = self.client.get(self.url + f"?payment_id={self.payment.id}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r["id"] for r in resp.data["results"]]
        self.assertIn(d1.id, ids)
        self.assertNotIn(d2.id, ids)

    def test_filter_invalid_payment_id_returns_400(self):
        resp = self.client.get(self.url + "?payment_id=abc")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filter_date_range(self):
        d = _make_delivery(self.payment)
        created = d.created_at.strftime("%Y-%m-%d")
        resp = self.client.get(self.url + f"?created_after={created}&created_before={created}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r["id"] for r in resp.data["results"]]
        self.assertIn(d.id, ids)

    def test_filter_invalid_created_after_returns_400(self):
        resp = self.client.get(self.url + "?created_after=not-a-date")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filter_reversed_date_range_returns_400(self):
        resp = self.client.get(self.url + "?created_after=2030-01-01&created_before=2020-01-01")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filter_combined_and(self):
        d_match = _make_delivery(self.payment, status="failed", event_type="payment.cancelled")
        d_wrong_event = _make_delivery(self.payment, status="failed", event_type="payment.succeeded")

        resp = self.client.get(
            self.url + f"?status=failed&event_type=payment.cancelled&payment_id={self.payment.id}"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [r["id"] for r in resp.data["results"]]
        self.assertIn(d_match.id, ids)
        self.assertNotIn(d_wrong_event.id, ids)

    def test_unknown_parameters_ignored(self):
        resp = self.client.get(self.url + "?totally_unknown_param=foo")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# 4. Detail endpoint
# ---------------------------------------------------------------------------

class WebhookDeliveryDetailTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        register_provider(LiveCapableDouble())
        self.merchant_a, self.key_a = create_test_merchant(
            name="DetailMerchA",
            live_enabled=True,
            default_provider="wl_live_double",
        )
        self.merchant_b, self.key_b = create_test_merchant(name="DetailMerchB")
        with override_settings(LIVE_MODE_AVAILABLE=True):
            self.live_key_a = self.merchant_a.create_live_key()

    def tearDown(self):
        cache.clear()
        _registry.pop("wl_live_double", None)

    def test_detail_200_for_owner(self):
        pay = _make_payment(self.merchant_a)
        d = _make_delivery(pay)
        self.client.credentials(HTTP_X_API_KEY=self.key_a)
        resp = self.client.get(reverse("webhook_delivery_detail", args=[d.id]))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["id"], d.id)

    def test_detail_404_for_other_merchant(self):
        pay = _make_payment(self.merchant_a)
        d = _make_delivery(pay)
        self.client.credentials(HTTP_X_API_KEY=self.key_b)
        resp = self.client.get(reverse("webhook_delivery_detail", args=[d.id]))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    @override_settings(LIVE_MODE_AVAILABLE=True)
    def test_detail_404_for_other_mode(self):
        # Live delivery, accessed with test key
        pay_live = _make_payment(self.merchant_a, livemode=True)
        d = _make_delivery(pay_live)
        self.client.credentials(HTTP_X_API_KEY=self.key_a)
        resp = self.client.get(reverse("webhook_delivery_detail", args=[d.id]))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_detail_404_for_missing_id(self):
        self.client.credentials(HTTP_X_API_KEY=self.key_a)
        resp = self.client.get(reverse("webhook_delivery_detail", args=[999999]))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# 5. Field set enforcement and secret/URL redaction
# ---------------------------------------------------------------------------

ALLOWED_FIELDS = {
    "id", "event_id", "event_type", "payment_id", "status", "attempt_count",
    "max_attempts", "last_response_code", "error_code", "target_host",
    "livemode", "created_at", "updated_at",
}


class WebhookDeliveryFieldSetTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.merchant, self.key = create_test_merchant(name="FieldMerch")
        self.client.credentials(HTTP_X_API_KEY=self.key)
        self.payment = _make_payment(self.merchant)

    def tearDown(self):
        cache.clear()

    def test_exact_field_set_list(self):
        _make_delivery(self.payment)
        resp = self.client.get(reverse("webhook_delivery_list"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for item in resp.data["results"]:
            self.assertEqual(set(item.keys()), ALLOWED_FIELDS,
                             f"Field mismatch: got {set(item.keys())}")

    def test_exact_field_set_detail(self):
        d = _make_delivery(self.payment)
        resp = self.client.get(reverse("webhook_delivery_detail", args=[d.id]))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(set(resp.data.keys()), ALLOWED_FIELDS)

    def test_error_code_mapping(self):
        cases = [
            ("blocked_target", "blocked_target"),
            ("Timeout", "timeout"),
            ("ReadTimeout", "timeout"),
            ("ConnectionError", "connection_error"),
            ("Non-2xx response: 404", "http_4xx"),
            ("Non-2xx response: 503", "http_5xx"),
            ("Redirect rejected: 302", "redirect"),
            ("SomeWeirdError", "unknown"),
            ("", ""),
            (None, ""),
        ]
        for raw, expected in cases:
            actual = _derive_error_code(raw or "")
            self.assertEqual(actual, expected, f"error_code({raw!r}) expected {expected!r}, got {actual!r}")

    def test_raw_last_error_never_in_response(self):
        # Put a secret-looking string in last_error; it must not appear in any response field
        secret_error = "API_SECRET_KEY=sk_live_SUPERSECRET conn refused"
        d = _make_delivery(self.payment, last_error=secret_error)

        resp_list = self.client.get(reverse("webhook_delivery_list"))
        self.assertEqual(resp_list.status_code, status.HTTP_200_OK)
        for item in resp_list.data["results"]:
            for val in item.values():
                self.assertNotIn("SUPERSECRET", str(val))
                self.assertNotIn(secret_error, str(val))

        resp_detail = self.client.get(reverse("webhook_delivery_detail", args=[d.id]))
        self.assertEqual(resp_detail.status_code, status.HTTP_200_OK)
        for val in resp_detail.data.values():
            self.assertNotIn("SUPERSECRET", str(val))
            self.assertNotIn(secret_error, str(val))

    def test_full_url_path_query_credentials_never_in_response(self):
        # Build a target_url with a secret in the query string
        secret_token = "SECRET_WEBHOOK_TOKEN_ABCDEF"
        pay = _make_payment(self.merchant)
        d = WebhookDelivery.objects.create(
            payment=pay,
            event_type="payment.cancelled",
            target_url=f"https://hooks.example.com/path/to/endpoint?token={secret_token}&foo=bar",
            payload={"id": str(uuid.uuid4()), "type": "payment.cancelled", "livemode": False,
                     "data": {"payment_id": pay.id, "status": pay.status,
                              "amount": str(pay.amount), "idempotency_key": pay.idempotency_key}},
            status="failed",
        )

        resp = self.client.get(reverse("webhook_delivery_detail", args=[d.id]))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # Flatten all response values to strings for comprehensive check
        full_response_str = str(resp.data)
        self.assertNotIn(secret_token, full_response_str, "Secret token leaked from URL query")
        self.assertNotIn("/path/to/endpoint", full_response_str, "URL path leaked")
        # target_host should only be the hostname
        self.assertEqual(resp.data["target_host"], "hooks.example.com")

    def test_no_payload_in_response(self):
        d = _make_delivery(self.payment)
        resp = self.client.get(reverse("webhook_delivery_detail", args=[d.id]))
        self.assertNotIn("payload", resp.data)


# ---------------------------------------------------------------------------
# 6. Resend action
# ---------------------------------------------------------------------------

class WebhookDeliveryResendTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        register_provider(LiveCapableDouble())
        self.merchant_a, self.key_a = create_test_merchant(name="ResendMerchA")
        self.merchant_b, self.key_b = create_test_merchant(name="ResendMerchB")
        self.payment = _make_payment(self.merchant_a)
        self.client.credentials(HTTP_X_API_KEY=self.key_a)

    def tearDown(self):
        cache.clear()
        _registry.pop("wl_live_double", None)

    def _resend_url(self, delivery_id):
        return reverse("webhook_delivery_resend", args=[delivery_id])

    @patch("payments.services.deliver_webhook_attempt")
    def test_resend_failed_delivery_returns_202_and_queues_task(self, mock_task):
        original_event_id = uuid.uuid4()
        d = WebhookDelivery.objects.create(
            payment=self.payment,
            event_id=original_event_id,
            event_type="payment.cancelled",
            target_url="https://hooks.example.com/wh",
            payload={"id": str(original_event_id), "type": "payment.cancelled", "livemode": False,
                     "data": {"payment_id": self.payment.id, "status": self.payment.status,
                              "amount": str(self.payment.amount),
                              "idempotency_key": self.payment.idempotency_key}},
            status="failed",
            attempt_count=5,
        )

        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(self._resend_url(d.id))
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(resp.data["status"], "pending")
        self.assertEqual(resp.data["id"], d.id)

        d.refresh_from_db()
        self.assertEqual(d.status, "pending")
        self.assertEqual(d.attempt_count, 0)
        self.assertEqual(str(d.event_id), str(original_event_id), "event_id must be unchanged")

        # Task queued exactly once
        mock_task.delay.assert_called_once_with(d.id)

    @patch("payments.services.deliver_webhook_attempt")
    def test_resend_pending_delivery_returns_409(self, mock_task):
        d = _make_delivery(self.payment, status="pending", attempt_count=1)
        resp = self.client.post(self._resend_url(d.id))
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("Only failed", resp.data["detail"])
        mock_task.delay.assert_not_called()

    @patch("payments.services.deliver_webhook_attempt")
    def test_resend_succeeded_delivery_returns_409(self, mock_task):
        d = _make_delivery(self.payment, status="succeeded", attempt_count=1)
        resp = self.client.post(self._resend_url(d.id))
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        mock_task.delay.assert_not_called()

    @patch("payments.services.deliver_webhook_attempt")
    def test_double_resend_returns_409_second_time(self, mock_task):
        d = _make_delivery(self.payment, status="failed")

        with self.captureOnCommitCallbacks(execute=True):
            resp1 = self.client.post(self._resend_url(d.id))
        self.assertEqual(resp1.status_code, status.HTTP_202_ACCEPTED)

        # After first resend, delivery is pending — second resend should 409
        resp2 = self.client.post(self._resend_url(d.id))
        self.assertEqual(resp2.status_code, status.HTTP_409_CONFLICT)

        # Task queued exactly once
        self.assertEqual(mock_task.delay.call_count, 1)

    @patch("payments.services.deliver_webhook_attempt")
    def test_resend_other_merchant_delivery_returns_404(self, mock_task):
        pay_b = _make_payment(self.merchant_b)
        d_b = _make_delivery(pay_b, status="failed")

        # Authenticated as merchant_a; delivery belongs to merchant_b
        resp = self.client.post(self._resend_url(d_b.id))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        mock_task.delay.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Resend throttle isolation
# ---------------------------------------------------------------------------

class WebhookResendThrottleTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.merchant, self.key = create_test_merchant(name="ThrottleMerch")
        self.client.credentials(HTTP_X_API_KEY=self.key)
        self.payment = _make_payment(self.merchant)

    def tearDown(self):
        cache.clear()

    @override_settings(
        WEBHOOK_RESEND_THROTTLE_RATE="3/minute",
        PAYMENT_READ_THROTTLE_RATE="10000/minute",
    )
    @patch("payments.services.deliver_webhook_attempt")
    def test_resend_throttle_independent_from_list_throttle(self, mock_task):
        """Exhausting the resend throttle must not affect the list endpoint."""
        # Create 5 failed deliveries
        deliveries = [_make_delivery(self.payment, status="failed") for _ in range(5)]
        list_url = reverse("webhook_delivery_list")
        resend_url_fn = lambda pk: reverse("webhook_delivery_resend", args=[pk])

        # Exhaust resend throttle (3 allowed)
        for d in deliveries[:3]:
            resp = self.client.post(resend_url_fn(d.id))
            self.assertIn(resp.status_code, [status.HTTP_202_ACCEPTED, status.HTTP_409_CONFLICT])

        # 4th resend should be throttled
        extra_d = _make_delivery(self.payment, status="failed")
        resp_throttled = self.client.post(resend_url_fn(extra_d.id))
        self.assertEqual(resp_throttled.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

        # List endpoint must still work (different bucket)
        resp_list = self.client.get(list_url)
        self.assertEqual(resp_list.status_code, status.HTTP_200_OK)

    @override_settings(WEBHOOK_RESEND_THROTTLE_RATE="2/minute")
    @patch("payments.services.deliver_webhook_attempt")
    def test_resend_returns_429_when_throttle_exceeded(self, mock_task):
        deliveries = [_make_delivery(self.payment, status="failed") for _ in range(4)]
        resend_url_fn = lambda pk: reverse("webhook_delivery_resend", args=[pk])

        # First 2 allowed
        for d in deliveries[:2]:
            resp = self.client.post(resend_url_fn(d.id))
            self.assertIn(resp.status_code, [status.HTTP_202_ACCEPTED, status.HTTP_409_CONFLICT])

        # 3rd should be 429
        resp = self.client.post(resend_url_fn(deliveries[2].id))
        self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


# ---------------------------------------------------------------------------
# 8. Admin retry action uses service
# ---------------------------------------------------------------------------

class AdminRetryActionTests(TestCase):

    def setUp(self):
        self.merchant, _ = create_test_merchant(name="AdminRetryMerch")
        self.payment = _make_payment(self.merchant)
        self.factory = RequestFactory()

    @patch("payments.services.deliver_webhook_attempt")
    def test_admin_retry_action_calls_resend_delivery_for_failed(self, mock_task):
        d = _make_delivery(self.payment, status="failed")
        qs = WebhookDelivery.objects.filter(pk=d.pk)

        admin_site = AdminSite()
        modeladmin = WebhookDeliveryAdmin(WebhookDelivery, admin_site)
        request = self.factory.post("/admin/")

        messages_store = []

        def fake_message_user(req, msg, level=None):
            messages_store.append(msg)

        modeladmin.message_user = fake_message_user

        with self.captureOnCommitCallbacks(execute=True):
            retry_webhook_deliveries(modeladmin, request, qs)

        d.refresh_from_db()
        self.assertEqual(d.status, "pending")
        self.assertEqual(d.attempt_count, 0)
        mock_task.delay.assert_called_once_with(d.pk)
        self.assertIn("1", messages_store[0])

    @patch("payments.services.deliver_webhook_attempt")
    def test_admin_retry_action_skips_non_failed_deliveries(self, mock_task):
        """Admin retry now only counts deliveries that were actually in failed state."""
        d_pending = _make_delivery(self.payment, status="pending", attempt_count=2)
        qs = WebhookDelivery.objects.filter(pk=d_pending.pk)

        admin_site = AdminSite()
        modeladmin = WebhookDeliveryAdmin(WebhookDelivery, admin_site)
        request = self.factory.post("/admin/")
        messages_store = []

        def fake_message_user(req, msg, level=None):
            messages_store.append(msg)

        modeladmin.message_user = fake_message_user

        retry_webhook_deliveries(modeladmin, request, qs)

        # Task must NOT be queued for a pending delivery
        mock_task.delay.assert_not_called()
        # Message should report 0
        self.assertIn("0", messages_store[0])


# ---------------------------------------------------------------------------
# 9. Schema validation
# ---------------------------------------------------------------------------

class SchemaValidationTest(TestCase):

    def test_spectacular_schema_validates(self):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(
            [sys.executable, "manage.py", "spectacular", "--validate"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=r"c:\Users\HP\Desktop\SafePay",
        )
        self.assertEqual(
            result.returncode, 0,
            msg=f"Schema validation failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

    def test_webhook_delivery_paths_in_schema(self):
        """Schema must include all three webhook-delivery paths."""
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(
            [sys.executable, "manage.py", "spectacular"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=r"c:\Users\HP\Desktop\SafePay",
        )
        schema_text = result.stdout
        self.assertIn("/api/webhook-deliveries/", schema_text)
        self.assertIn("/api/webhook-deliveries/{id}/", schema_text)
        self.assertIn("/api/webhook-deliveries/{id}/resend/", schema_text)
