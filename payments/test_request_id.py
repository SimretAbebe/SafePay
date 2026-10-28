import logging
import re
from unittest.mock import patch

from django.core.cache import cache
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.urls import path
from rest_framework import status
from rest_framework.test import APIClient

from payments.middleware import RequestIdFilter, RequestIdMiddleware, request_id_var
from payments.test_utils import create_test_merchant


def _error_view(request):
    raise RuntimeError("Forced explosion for 500 test")


def _ok_view(request):
    return HttpResponse("ok")


urlpatterns = [
    path("test-500/", _error_view),
    path("test-ok/", _ok_view),
]


class RequestIdTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def tearDown(self):
        cache.clear()

    def test_request_id_generated_when_absent(self):
        response = self.client.get("/health/")
        self.assertIn("X-Request-Id", response)
        rid = response["X-Request-Id"]
        self.assertEqual(len(rid), 32)
        self.assertTrue(re.match(r"^[0-9a-f]{32}$", rid))

    def test_request_id_echoed_when_valid(self):
        valid_ids = [
            "valid-request-id-1234",
            "req_ABC.123-xyz_89",
            "a" * 8,
            "z" * 64,
        ]
        for vid in valid_ids:
            response = self.client.get("/health/", HTTP_X_REQUEST_ID=vid)
            self.assertEqual(response["X-Request-Id"], vid)

    def test_request_id_replaced_when_invalid(self):
        invalid_ids = [
            "short",
            "x" * 65,
            "id with spaces 123",
            "id/with/slashes",
            "evil\r\nInjected-Header: 1",
            "bad@character#here$",
            "",
        ]
        for bad_id in invalid_ids:
            response = self.client.get("/health/", HTTP_X_REQUEST_ID=bad_id)
            rid = response["X-Request-Id"]
            self.assertNotEqual(rid, bad_id)
            self.assertEqual(len(rid), 32)
            self.assertTrue(re.match(r"^[0-9a-f]{32}$", rid))
            self.assertNotIn("\r", rid)
            self.assertNotIn("\n", rid)
            self.assertNotIn("Injected-Header", response)

    def _assert_has_exactly_one_request_id(self, response, expected_status=None):
        if expected_status is not None:
            self.assertEqual(response.status_code, expected_status)
        self.assertIn("X-Request-Id", response)
        matching = [k for k in response.headers if k.lower() == "x-request-id"]
        self.assertEqual(len(matching), 1)
        self.assertTrue(bool(response["X-Request-Id"]))

    def test_header_present_on_200_health(self):
        response = self.client.get("/health/")
        self._assert_has_exactly_one_request_id(response, 200)

    @patch("payments.health._check_broker", return_value="ok")
    @patch("payments.health._check_cache", return_value="ok")
    @patch("payments.health._check_database", return_value="ok")
    def test_header_present_on_200_ready(self, mock_db, mock_cache, mock_broker):
        response = self.client.get("/ready/")
        self._assert_has_exactly_one_request_id(response, 200)

    def test_header_present_on_400(self):
        merchant, api_key = create_test_merchant("ReqID Merchant 400")
        response = self.client.post(
            "/api/checkout/sessions/",
            {"amount": "100.00"},
            HTTP_X_API_KEY=api_key,
        )
        self._assert_has_exactly_one_request_id(response, 400)

    def test_header_present_on_403(self):
        response = self.client.get("/api/payments/")
        self._assert_has_exactly_one_request_id(response, 403)

    def test_header_present_on_404(self):
        response = self.client.get("/nonexistent-endpoint-404/")
        self._assert_has_exactly_one_request_id(response, 404)

    @override_settings(HEALTH_THROTTLE_RATE="1/minute", HEALTH_CACHE_SECONDS=0)
    def test_header_present_on_429(self):
        cache.clear()
        self.client.get("/health/")
        res_throttled = self.client.get("/health/")
        self._assert_has_exactly_one_request_id(res_throttled, 429)

    def test_header_present_on_forced_500(self):
        factory = RequestFactory()
        req = factory.get("/explode/")

        def _exploding_get_response(r):
            raise RuntimeError("Database connection catastrophically died")

        middleware = RequestIdMiddleware(_exploding_get_response)
        response = middleware(req)
        self.assertEqual(response.status_code, 500)
        self._assert_has_exactly_one_request_id(response, 500)

    def test_logging_filter_during_request_contains_request_id(self):
        factory = RequestFactory()
        test_id = "test-log-request-id-1234"
        req = factory.get("/test/", HTTP_X_REQUEST_ID=test_id)

        captured_records = []

        class TestCaptureHandler(logging.Handler):
            def emit(self, record):
                captured_records.append(record)

        test_logger = logging.getLogger("safepay.test_log_during")
        test_logger.setLevel(logging.INFO)
        handler = TestCaptureHandler()
        flt = RequestIdFilter()
        handler.addFilter(flt)
        test_logger.addHandler(handler)

        def _view_that_logs(r):
            test_logger.info("Handling request inside view")
            return HttpResponse("ok")

        try:
            middleware = RequestIdMiddleware(_view_that_logs)
            response = middleware(req)
            self.assertEqual(response.status_code, 200)

            self.assertEqual(len(captured_records), 1)
            record = captured_records[0]
            self.assertEqual(getattr(record, "request_id", None), test_id)

            formatter = logging.Formatter("[%(request_id)s] %(message)s")
            formatted = formatter.format(record)
            self.assertIn(f"[{test_id}]", formatted)
        finally:
            test_logger.removeHandler(handler)

    def test_logging_filter_outside_request_shows_dash(self):
        token = request_id_var.set("-")
        try:
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="test.py",
                lineno=1,
                msg="Log message outside request",
                args=(),
                exc_info=None,
            )
            flt = RequestIdFilter()
            flt.filter(record)

            self.assertEqual(record.request_id, "-")
            formatter = logging.Formatter("[%(request_id)s] %(message)s")
            formatted = formatter.format(record)
            self.assertIn("[-] Log message outside request", formatted)
        finally:
            request_id_var.reset(token)

    def test_json_body_does_not_contain_request_id(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("X-Request-Id", response)
        data = response.json()
        self.assertNotIn("request_id", data)
        self.assertNotIn("X-Request-Id", data)
        self.assertEqual(data, {"status": "ok"})
