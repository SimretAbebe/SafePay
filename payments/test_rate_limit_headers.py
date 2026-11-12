"""Tests for X-RateLimit-Limit, X-RateLimit-Remaining, and X-RateLimit-Reset headers."""
import uuid
from decimal import Decimal
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from payments.models import Payment
from payments.test_utils import create_test_merchant


@override_settings(
    PAYMENT_THROTTLE_RATE="5/minute",
    PAYMENT_READ_THROTTLE_RATE="10/minute",
    ENABLE_FAKE_PROVIDER=True,
    DEFAULT_PROVIDER="fake",
)
class RateLimitHeadersTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.merchant, self.raw_key = create_test_merchant("RateLimitMerchant")
        self.merchant_b, self.raw_key_b = create_test_merchant("OtherMerchant")

    def tearDown(self):
        cache.clear()

    def _auth_headers(self, raw_key=None, idemp_key=None):
        headers = {
            "HTTP_X_API_KEY": raw_key or self.raw_key,
        }
        if idemp_key:
            headers["HTTP_IDEMPOTENCY_KEY"] = idemp_key
        return headers

    def test_1_create_checkout_session_headers_and_remaining_decreases(self):
        """
        1. A create_checkout_session success includes all three headers with the
        correct limit, and remaining decreases by one on each call.
        """
        url = reverse("create_checkout_session")
        payload = {"amount": "100.00"}

        # Call 1
        resp1 = self.client.post(
            url,
            payload,
            format="json",
            **self._auth_headers(idemp_key=f"idemp-{uuid.uuid4().hex}"),
        )
        self.assertEqual(resp1.status_code, status.HTTP_201_CREATED)
        self.assertIn("X-RateLimit-Limit", resp1.headers)
        self.assertIn("X-RateLimit-Remaining", resp1.headers)
        self.assertIn("X-RateLimit-Reset", resp1.headers)

        limit1 = int(resp1.headers["X-RateLimit-Limit"])
        remaining1 = int(resp1.headers["X-RateLimit-Remaining"])
        reset1 = int(resp1.headers["X-RateLimit-Reset"])

        self.assertEqual(limit1, 5)
        self.assertEqual(remaining1, 4)
        self.assertGreater(reset1, 0)
        self.assertLessEqual(reset1, 60)

        # Call 2
        resp2 = self.client.post(
            url,
            payload,
            format="json",
            **self._auth_headers(idemp_key=f"idemp-{uuid.uuid4().hex}"),
        )
        self.assertEqual(resp2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(int(resp2.headers["X-RateLimit-Limit"]), 5)
        self.assertEqual(int(resp2.headers["X-RateLimit-Remaining"]), 3)
        self.assertGreater(int(resp2.headers["X-RateLimit-Reset"]), 0)

        # Call 3
        resp3 = self.client.post(
            url,
            payload,
            format="json",
            **self._auth_headers(idemp_key=f"idemp-{uuid.uuid4().hex}"),
        )
        self.assertEqual(resp3.status_code, status.HTTP_201_CREATED)
        self.assertEqual(int(resp3.headers["X-RateLimit-Remaining"]), 2)

    def test_2_list_payments_headers_separate_from_create_bucket(self):
        """
        2. A list payments call includes the reads bucket headers, separate from the create bucket.
        """
        url_reads = reverse("payment_list")
        url_create = reverse("create_payment")

        # Call create once (payments scope: limit 5)
        idemp = f"idemp-{uuid.uuid4().hex}"
        resp_create = self.client.post(
            url_create,
            {
                "amount": "50.00",
                "sender": "alice@example.com",
                "receiver": "merchant",
                "idempotency_key": idemp,
            },
            format="json",
            **self._auth_headers(idemp_key=idemp),
        )
        self.assertEqual(resp_create.status_code, status.HTTP_201_CREATED)
        self.assertEqual(int(resp_create.headers["X-RateLimit-Limit"]), 5)
        self.assertEqual(int(resp_create.headers["X-RateLimit-Remaining"]), 4)

        # Call reads (payment_reads scope: limit 10)
        resp_read = self.client.get(url_reads, **self._auth_headers())
        self.assertEqual(resp_read.status_code, status.HTTP_200_OK)
        self.assertIn("X-RateLimit-Limit", resp_read.headers)
        self.assertIn("X-RateLimit-Remaining", resp_read.headers)
        self.assertIn("X-RateLimit-Reset", resp_read.headers)

        self.assertEqual(int(resp_read.headers["X-RateLimit-Limit"]), 10)
        self.assertEqual(int(resp_read.headers["X-RateLimit-Remaining"]), 9)
        self.assertGreater(int(resp_read.headers["X-RateLimit-Reset"]), 0)

    def test_3_429_response_includes_headers_with_remaining_zero_and_positive_reset(self):
        """
        3. A 429 response includes the headers, with remaining 0 and a positive reset.
        """
        url = reverse("create_checkout_session")
        payload = {"amount": "100.00"}

        # Consume 5 allowed requests
        for i in range(5):
            res = self.client.post(
                url,
                payload,
                format="json",
                **self._auth_headers(idemp_key=f"idemp-{uuid.uuid4().hex}"),
            )
            self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        # 6th request triggers 429
        resp_429 = self.client.post(
            url,
            payload,
            format="json",
            **self._auth_headers(idemp_key=f"idemp-{uuid.uuid4().hex}"),
        )
        self.assertEqual(resp_429.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn("X-RateLimit-Limit", resp_429.headers)
        self.assertIn("X-RateLimit-Remaining", resp_429.headers)
        self.assertIn("X-RateLimit-Reset", resp_429.headers)

        self.assertEqual(int(resp_429.headers["X-RateLimit-Limit"]), 5)
        self.assertEqual(int(resp_429.headers["X-RateLimit-Remaining"]), 0)
        self.assertGreater(int(resp_429.headers["X-RateLimit-Reset"]), 0)

    def test_4_remaining_never_goes_below_zero_after_extra_requests(self):
        """
        4. Remaining never goes below zero, including after several extra requests.
        """
        url = reverse("create_checkout_session")
        payload = {"amount": "100.00"}

        # Exhaust bucket (5 calls)
        for _ in range(5):
            self.client.post(
                url,
                payload,
                format="json",
                **self._auth_headers(idemp_key=f"idemp-{uuid.uuid4().hex}"),
            )

        # Make 4 additional throttled requests
        for _ in range(4):
            resp = self.client.post(
                url,
                payload,
                format="json",
                **self._auth_headers(idemp_key=f"idemp-{uuid.uuid4().hex}"),
            )
            self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
            remaining = int(resp.headers["X-RateLimit-Remaining"])
            self.assertEqual(remaining, 0)
            self.assertGreaterEqual(int(resp.headers["X-RateLimit-Reset"]), 0)

    def test_5_unrelated_errors_missing_key_and_other_merchant_404_no_headers(self):
        """
        5. A 404 for another merchant's payment and a 403 for a missing key do NOT include the headers.
        """
        # Missing API key on throttled endpoint -> 403 Forbidden
        url_reads = reverse("payment_list")
        resp_no_key = self.client.get(url_reads)
        self.assertEqual(resp_no_key.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotIn("X-RateLimit-Limit", resp_no_key.headers)
        self.assertNotIn("X-RateLimit-Remaining", resp_no_key.headers)
        self.assertNotIn("X-RateLimit-Reset", resp_no_key.headers)

        # Invalid key -> 403 Forbidden
        resp_bad_key = self.client.get(url_reads, HTTP_X_API_KEY="sp_test_invalidkey123")
        self.assertEqual(resp_bad_key.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotIn("X-RateLimit-Limit", resp_bad_key.headers)
        self.assertNotIn("X-RateLimit-Remaining", resp_bad_key.headers)
        self.assertNotIn("X-RateLimit-Reset", resp_bad_key.headers)

        # Payment belonging to Merchant B
        payment_b = Payment.objects.create(
            merchant=self.merchant_b,
            amount=Decimal("100.00"),
            sender="alice",
            receiver="bob",
            provider="fake",
            status="pending",
            idempotency_key="idemp-b-404-check",
            livemode=False,
        )

        # Merchant A requests Merchant B's payment -> 404
        url_detail = reverse("payment_detail", kwargs={"pk": payment_b.pk})
        resp_404 = self.client.get(url_detail, **self._auth_headers(raw_key=self.raw_key))
        self.assertEqual(resp_404.status_code, status.HTTP_404_NOT_FOUND)
        self.assertNotIn("X-RateLimit-Limit", resp_404.headers)
        self.assertNotIn("X-RateLimit-Remaining", resp_404.headers)
        self.assertNotIn("X-RateLimit-Reset", resp_404.headers)

    def test_6_health_and_ready_endpoints_do_not_include_headers(self):
        """
        6. /health/ and /ready/ do NOT include the headers.
        """
        resp_health = self.client.get("/health/")
        self.assertEqual(resp_health.status_code, status.HTTP_200_OK)
        self.assertNotIn("X-RateLimit-Limit", resp_health.headers)
        self.assertNotIn("X-RateLimit-Remaining", resp_health.headers)
        self.assertNotIn("X-RateLimit-Reset", resp_health.headers)

        resp_ready = self.client.get("/ready/")
        # /ready/ may return 200 or 503 depending on external service availability in
        # the test environment (Redis, Celery). Either way, rate-limit headers must be absent.
        self.assertIn(resp_ready.status_code, [status.HTTP_200_OK, status.HTTP_503_SERVICE_UNAVAILABLE])
        self.assertNotIn("X-RateLimit-Limit", resp_ready.headers)
        self.assertNotIn("X-RateLimit-Remaining", resp_ready.headers)
        self.assertNotIn("X-RateLimit-Reset", resp_ready.headers)

    def test_7_headers_appear_exactly_once_per_response(self):
        """
        7. The headers appear exactly once per response (count them).
        """
        url = reverse("payment_list")
        resp = self.client.get(url, **self._auth_headers())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # Count header occurrences in raw response headers
        header_keys = [k.lower() for k in resp.headers.keys()]
        self.assertEqual(header_keys.count("x-ratelimit-limit"), 1)
        self.assertEqual(header_keys.count("x-ratelimit-remaining"), 1)
        self.assertEqual(header_keys.count("x-ratelimit-reset"), 1)

    def test_8_body_and_status_of_existing_responses_unchanged(self):
        """
        8. The body and status of existing responses are unchanged.
        """
        url = reverse("create_checkout_session")
        payload = {"amount": "150.00"}
        idemp = f"idemp-{uuid.uuid4().hex}"

        resp = self.client.post(
            url,
            payload,
            format="json",
            **self._auth_headers(idemp_key=idemp),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIn("id", resp.data)
        self.assertEqual(resp.data["status"], "pending")
        self.assertIn("checkout_url", resp.data)
        self.assertEqual(resp.data["livemode"], False)

        # Idempotent replay response body and status
        replay_resp = self.client.post(
            url,
            payload,
            format="json",
            **self._auth_headers(idemp_key=idemp),
        )
        self.assertEqual(replay_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(replay_resp.headers["Idempotent-Replayed"], "true")
        self.assertEqual(replay_resp.data["id"], resp.data["id"])

    def test_9_exhausting_one_bucket_does_not_change_numbers_of_another_bucket(self):
        """
        9. Exhausting one bucket does not change the numbers of another bucket.
        """
        url_create = reverse("create_payment")
        url_reads = reverse("payment_list")

        # Exhaust create bucket (limit 5)
        for _ in range(5):
            idemp = f"idemp-{uuid.uuid4().hex}"
            res = self.client.post(
                url_create,
                {
                    "amount": "20.00",
                    "sender": "alice@example.com",
                    "receiver": "merchant",
                    "idempotency_key": idemp,
                },
                format="json",
                **self._auth_headers(idemp_key=idemp),
            )
            self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        # 6th create fails with 429
        idemp_429 = f"idemp-{uuid.uuid4().hex}"
        res_create_throttled = self.client.post(
            url_create,
            {
                "amount": "20.00",
                "sender": "alice@example.com",
                "receiver": "merchant",
                "idempotency_key": idemp_429,
            },
            format="json",
            **self._auth_headers(idemp_key=idemp_429),
        )
        self.assertEqual(res_create_throttled.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(int(res_create_throttled.headers["X-RateLimit-Remaining"]), 0)

        # Reads bucket is still fresh (limit 10, remaining 9 on first read)
        res_read = self.client.get(url_reads, **self._auth_headers())
        self.assertEqual(res_read.status_code, status.HTTP_200_OK)
        self.assertEqual(int(res_read.headers["X-RateLimit-Limit"]), 10)
        self.assertEqual(int(res_read.headers["X-RateLimit-Remaining"]), 9)
        self.assertGreater(int(res_read.headers["X-RateLimit-Reset"]), 0)

