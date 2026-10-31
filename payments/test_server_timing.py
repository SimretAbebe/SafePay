import json
import logging
import re
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from payments.models import Merchant, Payment
from payments.providers.fake import FakeProvider
from payments.test_utils import create_test_merchant
from payments.timing import provider_timer


class FakeClock:
    """Deterministic fake clock for performance counter and sleep simulation."""

    def __init__(self, start=100.0, step=0.001):
        self.now = float(start)
        self.step = float(step)

    def perf_counter(self):
        val = self.now
        self.now += self.step
        return val

    def sleep(self, seconds):
        self.now += float(seconds)

    def advance(self, seconds):
        self.now += float(seconds)


@override_settings(
    SERVER_TIMING_ENABLED=True,
    SLOW_REQUEST_MS=800,
    ENABLE_FAKE_PROVIDER=True,
    DEFAULT_PROVIDER="fake",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class ServerTimingTests(TestCase):
    def setUp(self):
        cache.clear()
        FakeProvider.reset()
        self.client = APIClient()

        self.merchant, self.test_key = create_test_merchant(
            name="Timing Test Merchant",
            default_provider="fake",
        )
        self.payment = Payment.objects.create(
            merchant=self.merchant,
            amount="120.00",
            sender="alice@example.com",
            receiver="bob@example.com",
            idempotency_key="timing-test-pay-1",
            status="pending",
            livemode=False,
        )

    def tearDown(self):
        cache.clear()
        FakeProvider.reset()

    # 1. With the setting on, a normal request returns exactly one Server-Timing header
    #    matching the format (assert with a regex), with total >= db + provider.
    def test_server_timing_format_and_totals(self):
        url = reverse("payment_detail", kwargs={"pk": self.payment.pk})
        response = self.client.get(url, HTTP_X_API_KEY=self.test_key)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Server-Timing", response.headers)

        header_val = response.headers["Server-Timing"]
        # Format: db;dur=<ms>;desc="<n> queries", app;dur=<ms>, total;dur=<ms>
        pattern = r'^db;dur=(?P<db>\d+(\.\d+)?);desc="(?P<queries>\d+) queries", app;dur=(?P<app>\d+(\.\d+)?), total;dur=(?P<total>\d+(\.\d+)?)$'
        match = re.match(pattern, header_val)
        self.assertIsNotNone(match, f"Header did not match expected regex: {header_val}")

        db = float(match.group("db"))
        total = float(match.group("total"))
        self.assertGreaterEqual(total, db)

        # Ensure exactly one Server-Timing header
        count = sum(1 for k in response.headers.keys() if k.lower() == "server-timing")
        self.assertEqual(count, 1)

    # 2. With the setting off, no Server-Timing header is present, but the slow log still works.
    def test_setting_off_omits_header_but_slow_log_still_works(self):
        url = reverse("payment_detail", kwargs={"pk": self.payment.pk})

        clock = FakeClock(start=100.0, step=0.5)  # 500ms per perf_counter call -> total > 800ms
        with override_settings(SERVER_TIMING_ENABLED=False, SLOW_REQUEST_MS=400):
            with patch("payments.timing.time.perf_counter", side_effect=clock.perf_counter):
                with self.assertLogs("safepay.slow", level="WARNING") as captured_logs:
                    response = self.client.get(url, HTTP_X_API_KEY=self.test_key)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertNotIn("Server-Timing", response.headers)

            # Slow log still emitted
            self.assertEqual(len(captured_logs.records), 1)
            self.assertIn(f"/api/payments/{self.payment.pk}/", captured_logs.output[0])

    # 3. The db metric counts the queries of the request (compare with assertNumQueries).
    def test_db_metric_counts_queries(self):
        url = reverse("payment_list")
        with self.assertNumQueries(2):
            response = self.client.get(url, HTTP_X_API_KEY=self.test_key)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        header_val = response.headers.get("Server-Timing", "")
        self.assertIn('desc="2 queries"', header_val)

    # 4. The provider metric appears when a checkout calls a provider double that sleeps (use a fake clock),
    #    and is absent when no provider was called; a failing provider call still records its time.
    def test_provider_metric_presence_and_failure(self):
        # 4a. No provider called -> provider metric absent
        detail_url = reverse("payment_detail", kwargs={"pk": self.payment.pk})
        res_no_provider = self.client.get(detail_url, HTTP_X_API_KEY=self.test_key)
        self.assertEqual(res_no_provider.status_code, status.HTTP_200_OK)
        self.assertNotIn("provider;dur=", res_no_provider.headers.get("Server-Timing", ""))

        # 4b. Provider called with simulated latency -> provider metric present
        checkout_url = reverse("create_checkout_session")
        payload = {
            "amount": "150.00",
            "reference": "ORDER-TIMING-1",
            "return_url": "https://example.com/complete",
            "provider": "fake",
        }

        clock = FakeClock()
        with patch("payments.timing.time.perf_counter", side_effect=clock.perf_counter):
            with patch("payments.providers.fake.time.sleep", side_effect=clock.sleep):
                with patch.dict("os.environ", {"FAKE_PROVIDER_DELAY_MS": "300"}):
                    res_provider = self.client.post(
                        checkout_url,
                        payload,
                        format="json",
                        HTTP_X_API_KEY=self.test_key,
                        HTTP_IDEMPOTENCY_KEY="timing-idem-1",
                    )

        self.assertEqual(res_provider.status_code, status.HTTP_201_CREATED)
        timing_val = res_provider.headers.get("Server-Timing", "")
        self.assertIn("provider;dur=", timing_val)
        # Parse provider duration
        prov_match = re.search(r'provider;dur=(?P<pdur>\d+(\.\d+)?)', timing_val)
        self.assertIsNotNone(prov_match)
        self.assertGreaterEqual(float(prov_match.group("pdur")), 290.0)

        # 4c. Failing provider call still records time
        FakeProvider.set_error(infrastructure=True)
        clock2 = FakeClock()
        with patch("payments.timing.time.perf_counter", side_effect=clock2.perf_counter):
            with patch("payments.providers.fake.time.sleep", side_effect=clock2.sleep):
                with patch.dict("os.environ", {"FAKE_PROVIDER_DELAY_MS": "150"}):
                    res_failing = self.client.post(
                        checkout_url,
                        payload,
                        format="json",
                        HTTP_X_API_KEY=self.test_key,
                        HTTP_IDEMPOTENCY_KEY="timing-idem-fail",
                    )

        self.assertEqual(res_failing.status_code, status.HTTP_502_BAD_GATEWAY)
        fail_timing = res_failing.headers.get("Server-Timing", "")
        self.assertIn("provider;dur=", fail_timing)

    # 5. The slow log is emitted above the threshold with the request id and without the query string,
    #    and not emitted below it; /health/ and /ready/ never log; the log line contains no API key,
    #    no header value and no body.
    def test_slow_request_logging(self):
        url = reverse("payment_list")
        secret_query_val = "secret_query_param_xyz"
        secret_header_val = "secret_header_token_abc"
        secret_key_val = self.test_key

        # 5a. Fast request below threshold: no warning logged
        clock = FakeClock(step=0.001)
        with patch("payments.timing.time.perf_counter", side_effect=clock.perf_counter):
            with self.assertLogs("safepay.slow", level="WARNING") as captured_logs:
                # Log a canary line to prevent assertLogs failure when empty
                logging.getLogger("safepay.slow").warning("canary")
                res_fast = self.client.get(
                    f"{url}?filter={secret_query_val}",
                    HTTP_X_API_KEY=secret_key_val,
                    HTTP_X_CUSTOM_SECRET=secret_header_val,
                )
        self.assertEqual(res_fast.status_code, status.HTTP_200_OK)
        # Only the canary line should be present
        self.assertEqual(len(captured_logs.records), 1)

        # 5b. Slow request above threshold: log emitted with request id, no secrets
        clock_slow = FakeClock(start=100.0, step=0.5)  # 500ms per tick -> total > 800ms
        with patch("payments.timing.time.perf_counter", side_effect=clock_slow.perf_counter):
            with self.assertLogs("safepay.slow", level="WARNING") as captured_slow:
                res_slow = self.client.get(
                    f"{url}?filter={secret_query_val}",
                    HTTP_X_API_KEY=secret_key_val,
                    HTTP_X_CUSTOM_SECRET=secret_header_val,
                )

        self.assertEqual(res_slow.status_code, status.HTTP_200_OK)
        self.assertEqual(len(captured_slow.records), 1)
        log_line = captured_slow.output[0]

        rid = res_slow.headers.get("X-Request-Id")
        self.assertIn(rid, log_line)
        self.assertIn("/api/payments/", log_line)
        self.assertNotIn(secret_query_val, log_line)
        self.assertNotIn(secret_header_val, log_line)
        self.assertNotIn(secret_key_val, log_line)

        # 5c. /health/ and /ready/ never log even when slow
        for probe_path in ("/health/", "/ready/"):
            clock_probe = FakeClock(start=100.0, step=0.6)
            with patch("payments.timing.time.perf_counter", side_effect=clock_probe.perf_counter):
                with self.assertLogs("safepay.slow", level="WARNING") as captured_probe:
                    logging.getLogger("safepay.slow").warning("canary_probe")
                    self.client.get(probe_path)

            self.assertEqual(len(captured_probe.records), 1)

    # 6. Error responses (403, 404, 429) carry exactly one Server-Timing header
    #    and the existing X-Request-Id header exactly once.
    def test_error_responses_carry_server_timing_and_x_request_id(self):
        url = reverse("payment_detail", kwargs={"pk": self.payment.pk})

        # 403 Forbidden
        res_403 = self.client.get(url, HTTP_X_API_KEY="bad-key")
        self.assertEqual(res_403.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Server-Timing", res_403.headers)
        self.assertIn("X-Request-Id", res_403.headers)
        self.assertEqual(sum(1 for k in res_403.headers.keys() if k.lower() == "server-timing"), 1)
        self.assertEqual(sum(1 for k in res_403.headers.keys() if k.lower() == "x-request-id"), 1)

        # 404 Not Found
        bad_url = reverse("payment_detail", kwargs={"pk": 999999})
        res_404 = self.client.get(bad_url, HTTP_X_API_KEY=self.test_key)
        self.assertEqual(res_404.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("Server-Timing", res_404.headers)
        self.assertIn("X-Request-Id", res_404.headers)
        self.assertEqual(sum(1 for k in res_404.headers.keys() if k.lower() == "server-timing"), 1)
        self.assertEqual(sum(1 for k in res_404.headers.keys() if k.lower() == "x-request-id"), 1)

        # 429 Too Many Requests
        cache.clear()
        with override_settings(PAYMENT_READ_THROTTLE_RATE="1/minute"):
            # 1st request ok
            r1 = self.client.get(url, HTTP_X_API_KEY=self.test_key)
            self.assertEqual(r1.status_code, status.HTTP_200_OK)
            # 2nd request throttled
            r2 = self.client.get(url, HTTP_X_API_KEY=self.test_key)
            self.assertEqual(r2.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
            self.assertIn("Server-Timing", r2.headers)
            self.assertIn("X-Request-Id", r2.headers)
            self.assertEqual(sum(1 for k in r2.headers.keys() if k.lower() == "server-timing"), 1)
            self.assertEqual(sum(1 for k in r2.headers.keys() if k.lower() == "x-request-id"), 1)
