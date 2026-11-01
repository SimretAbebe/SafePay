"""Tests for the bench management command and bench_stats module."""

import json
import os
import tempfile
from decimal import Decimal
from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from payments.bench_stats import histogram, percentile, render_histogram, summarize
from payments.models import Merchant, Payment


def mock_response(status_code=200, headers=None, json_data=None):
    """Helper creating a mock HTTP response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = json_data or {}
    resp.text = json.dumps(json_data or {})
    return resp


class TestPercentile(TestCase):
    """Nearest-rank percentile calculations."""

    def test_empty_list_returns_none(self):
        self.assertIsNone(percentile([], 50))

    def test_single_value(self):
        self.assertEqual(percentile([42.0], 50), 42.0)
        self.assertEqual(percentile([42.0], 99), 42.0)

    def test_known_p50(self):
        self.assertEqual(percentile([15, 20, 35, 40, 50], 50), 35)

    def test_known_p95_and_p99(self):
        data = list(range(1, 101))
        self.assertEqual(percentile(data, 95), 95)
        self.assertEqual(percentile(data, 99), 99)

    def test_unsorted_input_not_mutated(self):
        data = [50, 10, 30, 20, 40]
        copy = list(data)
        self.assertEqual(percentile(data, 50), 30)
        self.assertEqual(data, copy)


class TestSummarize(TestCase):
    """Summarize function computes summary metrics."""

    def test_basic_metrics(self):
        result = summarize([10.0, 20.0, 30.0, 40.0, 50.0], errors=1, throttled=2, wall_seconds=5.0)
        self.assertEqual(result["count"], 5)
        self.assertAlmostEqual(result["mean"], 30.0)
        self.assertEqual(result["min"], 10.0)
        self.assertEqual(result["max"], 50.0)
        self.assertAlmostEqual(result["requests_per_second"], 1.0)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["throttled"], 2)

    def test_empty_durations(self):
        result = summarize([], errors=0, throttled=0, wall_seconds=1.0)
        self.assertEqual(result["count"], 0)
        self.assertIsNone(result["mean"])
        self.assertIsNone(result["p50"])
        self.assertEqual(result["requests_per_second"], 0)


class TestHistogram(TestCase):
    """Histogram bucket building and ASCII rendering."""

    def test_bucket_counts_sum(self):
        data = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        buckets = histogram(data, buckets=5)
        self.assertEqual(sum(c for _, _, c in buckets), len(data))

    def test_identical_values(self):
        buckets = histogram([5.0] * 20, buckets=10)
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0][2], 20)

    def test_empty_input(self):
        self.assertEqual(histogram([]), [])

    def test_rendered_bar_max_40_wide(self):
        buckets = histogram([float(x) for x in range(1, 101)], buckets=5)
        for line in render_histogram(buckets).strip().split("\n"):
            hashes = line.split("|")[1].count("#") if "|" in line else 0
            self.assertLessEqual(hashes, 40)


class TestSafetyChecks(TestCase):
    """Command safety guards against non-local or production usage."""

    def test_non_local_url_refused(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("bench", base_url="http://remote-server.com:8000")
        self.assertIn("non-local", str(ctx.exception))

    @override_settings(DEBUG=False)
    def test_debug_false_refused(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("bench", base_url="http://127.0.0.1:8000")
        self.assertIn("DEBUG is False", str(ctx.exception))

    @override_settings(DEBUG=True, LIVE_MODE_AVAILABLE=True)
    def test_live_mode_refused(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("bench", base_url="http://127.0.0.1:8000")
        self.assertIn("LIVE_MODE_AVAILABLE", str(ctx.exception))

    @override_settings(DEBUG=True, LIVE_MODE_AVAILABLE=False)
    def test_caps_requests_and_concurrency(self):
        err = StringIO()
        try:
            call_command("bench", requests=9999, concurrency=99, stderr=err)
        except Exception:
            pass
        self.assertIn("5000", err.getvalue())
        self.assertIn("32", err.getvalue())


@override_settings(DEBUG=True, LIVE_MODE_AVAILABLE=False)
class TestCommandRun(TestCase):
    """Command execution with mocked HTTP sessions."""

    @patch("payments.management.commands.bench.requests")
    def test_list_scenario_prints_table(self, mock_requests):
        session = MagicMock()
        session.request.return_value = mock_response(
            200, headers={"Server-Timing": 'db;dur=1.5;desc="2 queries", app;dur=2.0'}
        )
        mock_requests.Session.return_value = session

        out = StringIO()
        call_command("bench", scenario="list", requests=5, concurrency=1, warmup=0, stdout=out)
        output = out.getvalue()
        self.assertIn("list", output)
        self.assertIn("p50", output)
        self.assertIn("p95", output)

    @patch("payments.management.commands.bench.requests")
    def test_429_counted_as_throttled(self, mock_requests):
        counts = {"n": 0}

        def mock_req(*args, **kwargs):
            counts["n"] += 1
            return mock_response(429 if counts["n"] % 2 == 0 else 200)

        session = MagicMock()
        session.request.side_effect = mock_req
        mock_requests.Session.return_value = session

        out = StringIO()
        call_command("bench", scenario="list", requests=6, concurrency=1, warmup=0, stdout=out)
        self.assertIn("list", out.getvalue())

    @patch("payments.management.commands.bench.requests")
    def test_all_429_stops_with_throttle_guidance(self, mock_requests):
        session = MagicMock()
        session.request.return_value = mock_response(429)
        mock_requests.Session.return_value = session

        with self.assertRaises(CommandError) as ctx:
            call_command("bench", scenario="list", requests=15, concurrency=1, warmup=0)
        self.assertIn("429", str(ctx.exception))
        self.assertIn("PAYMENT_THROTTLE_RATE", str(ctx.exception))

    @patch("payments.management.commands.bench.requests")
    def test_errors_excluded_from_latency(self, mock_requests):
        counts = {"n": 0}

        def mock_req(*args, **kwargs):
            counts["n"] += 1
            return mock_response(500 if counts["n"] == 1 else 200)

        session = MagicMock()
        session.request.side_effect = mock_req
        mock_requests.Session.return_value = session

        out = StringIO()
        call_command("bench", scenario="list", requests=4, concurrency=1, warmup=0, stdout=out)
        self.assertIn("list", out.getvalue())


@override_settings(DEBUG=True, LIVE_MODE_AVAILABLE=False)
class TestCleanup(TestCase):
    """Bench merchant and payments are cleaned up in all cases."""

    @patch("payments.management.commands.bench.requests")
    def test_cleanup_after_success(self, mock_requests):
        other_m, _ = Merchant.create_with_key(name="keep-me")
        other_p = Payment.objects.create(
            merchant=other_m, idempotency_key="keep-1", amount=Decimal("10.00"), sender="a", receiver="b"
        )
        session = MagicMock()
        session.request.return_value = mock_response(200)
        mock_requests.Session.return_value = session

        call_command("bench", scenario="list", requests=2, concurrency=1, warmup=0, stdout=StringIO())

        self.assertFalse(Merchant.objects.filter(name__startswith="bench-").exists())
        self.assertTrue(Merchant.objects.filter(pk=other_m.pk).exists())
        self.assertTrue(Payment.objects.filter(pk=other_p.pk).exists())

    @patch("payments.management.commands.bench.requests")
    def test_cleanup_on_error(self, mock_requests):
        other_m, _ = Merchant.create_with_key(name="keep-me-too")
        session = MagicMock()
        session.request.return_value = mock_response(429)
        mock_requests.Session.return_value = session

        with self.assertRaises(CommandError):
            call_command("bench", scenario="list", requests=15, concurrency=1, warmup=0, stdout=StringIO())

        self.assertFalse(Merchant.objects.filter(name__startswith="bench-").exists())
        self.assertTrue(Merchant.objects.filter(pk=other_m.pk).exists())


@override_settings(DEBUG=True, LIVE_MODE_AVAILABLE=False)
class TestSecurity(TestCase):
    """Raw API keys must never appear in stdout, stderr, or JSON output."""

    @patch("payments.management.commands.bench.requests")
    def test_raw_key_never_printed(self, mock_requests):
        session = MagicMock()
        session.request.return_value = mock_response(200)
        session.headers = {}
        mock_requests.Session.return_value = session

        created_keys = []
        orig_create = Merchant.create_with_key.__func__

        def spy_create(cls, **kwargs):
            m, key = orig_create(cls, **kwargs)
            created_keys.append(key)
            return m, key

        out, err = StringIO(), StringIO()
        with patch.object(Merchant, "create_with_key", classmethod(spy_create)):
            call_command("bench", scenario="list", requests=2, concurrency=1, warmup=0, stdout=out, stderr=err)

        output = out.getvalue() + err.getvalue()
        self.assertTrue(created_keys)
        for key in created_keys:
            self.assertNotIn(key, output)

    @patch("payments.management.commands.bench.requests")
    def test_raw_key_not_in_json(self, mock_requests):
        session = MagicMock()
        session.request.return_value = mock_response(200)
        session.headers = {}
        mock_requests.Session.return_value = session

        created_keys = []
        orig_create = Merchant.create_with_key.__func__

        def spy_create(cls, **kwargs):
            m, key = orig_create(cls, **kwargs)
            created_keys.append(key)
            return m, key

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            json_path = tf.name

        try:
            with patch.object(Merchant, "create_with_key", classmethod(spy_create)):
                call_command("bench", scenario="list", requests=2, concurrency=1, warmup=0, json_path=json_path)

            with open(json_path) as f:
                content = f.read()

            data = json.loads(content)
            self.assertIn("list", data)
            for key in created_keys:
                self.assertNotIn(key, content)
        finally:
            os.unlink(json_path)


@override_settings(DEBUG=True, LIVE_MODE_AVAILABLE=False)
class TestBudgetAndProvider(TestCase):
    """Budget checking and provider error detection."""

    @patch("payments.management.commands.bench.requests")
    @patch("payments.management.commands.bench.time")
    def test_budget_exceeded_exits_1(self, mock_time, mock_requests):
        timer = {"t": 0.0}

        def fake_perf():
            timer["t"] += 0.5  # 500ms
            return timer["t"]

        mock_time.perf_counter = fake_perf
        session = MagicMock()
        session.request.return_value = mock_response(200)
        mock_requests.Session.return_value = session

        with self.assertRaises(CommandError) as ctx:
            call_command("bench", scenario="list", requests=3, concurrency=1, warmup=0, budget_p95_ms=50.0)
        self.assertIn("Budget exceeded", str(ctx.exception))

    @patch("payments.management.commands.bench.requests")
    def test_budget_met_passes(self, mock_requests):
        session = MagicMock()
        session.request.return_value = mock_response(200)
        mock_requests.Session.return_value = session

        out = StringIO()
        call_command("bench", scenario="list", requests=2, concurrency=1, warmup=0, budget_p95_ms=10000.0, stdout=out)
        self.assertIn("within p95 budget", out.getvalue())

    @patch("payments.management.commands.bench.requests")
    def test_disabled_provider_message(self, mock_requests):
        session = MagicMock()
        session.request.return_value = mock_response(
            400, json_data={"detail": "Unknown or unavailable provider: 'fake'"}
        )
        mock_requests.Session.return_value = session

        with self.assertRaises(CommandError) as ctx:
            call_command("bench", scenario="checkout", requests=2, concurrency=1, warmup=0)
        self.assertIn("ENABLE_FAKE_PROVIDER", str(ctx.exception))
