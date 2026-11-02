"""Tests for Server-Timing response header middleware."""

import re

from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from payments.test_utils import create_test_merchant


class ServerTimingHeaderTests(TestCase):
    """Verify Server-Timing response header behavior and format."""

    def setUp(self):
        self.client = APIClient()
        self.merchant, self.test_key = create_test_merchant(name="Timing Header Merch")

    def test_normal_200_response_has_valid_non_negative_server_timing(self):
        """1 & 2: A normal 200 response has exactly one Server-Timing header matching app;dur=<number> (not negative)."""
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Exactly one Server-Timing header
        self.assertIn("Server-Timing", response.headers)
        timing_headers = [k for k in response.headers.keys() if k.lower() == "server-timing"]
        self.assertEqual(len(timing_headers), 1)

        val = response.headers["Server-Timing"]
        match = re.match(r"^app;dur=(\d+(\.\d+)?)$", val)
        self.assertIsNotNone(match, f"Server-Timing header '{val}' did not match 'app;dur=<number>'")

        dur = float(match.group(1))
        self.assertGreaterEqual(dur, 0.0, "Duration must not be negative")

    def test_authenticated_200_has_server_timing(self):
        """Authenticated 200 response has valid Server-Timing header."""
        response = self.client.get("/api/payments/", HTTP_X_API_KEY=self.test_key)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        val = response.headers.get("Server-Timing", "")
        match = re.match(r"^app;dur=(\d+(\.\d+)?)$", val)
        self.assertIsNotNone(match, f"Expected app;dur=<number>, got: '{val}'")
        self.assertGreaterEqual(float(match.group(1)), 0.0)

    def test_404_and_401_responses_have_server_timing(self):
        """3: 404 and 401 error responses also carry the Server-Timing header."""
        # 401 Unauthorized (webhook missing required signature)
        resp_401 = self.client.post("/api/webhooks/chapa/", data=b"{}", content_type="application/json")
        self.assertEqual(resp_401.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("Server-Timing", resp_401.headers)
        match_401 = re.match(r"^app;dur=(\d+(\.\d+)?)$", resp_401.headers["Server-Timing"])
        self.assertIsNotNone(match_401, f"401 missing valid Server-Timing: {resp_401.headers.get('Server-Timing')}")

        # 404 Not Found
        resp_404 = self.client.get("/non-existent-endpoint-404/")
        self.assertEqual(resp_404.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("Server-Timing", resp_404.headers)
        match_404 = re.match(r"^app;dur=(\d+(\.\d+)?)$", resp_404.headers["Server-Timing"])
        self.assertIsNotNone(match_404, f"404 missing valid Server-Timing: {resp_404.headers.get('Server-Timing')}")

    @override_settings(SERVER_TIMING_ENABLED=False)
    def test_server_timing_disabled_omits_header(self):
        """4: With SERVER_TIMING_ENABLED=False, the Server-Timing header is absent."""
        resp_200 = self.client.get("/health/")
        self.assertEqual(resp_200.status_code, status.HTTP_200_OK)
        self.assertNotIn("Server-Timing", resp_200.headers)

        resp_401 = self.client.post("/api/webhooks/chapa/", data=b"{}", content_type="application/json")
        self.assertEqual(resp_401.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertNotIn("Server-Timing", resp_401.headers)

        resp_404 = self.client.get("/non-existent-endpoint-404/")
        self.assertEqual(resp_404.status_code, status.HTTP_404_NOT_FOUND)
        self.assertNotIn("Server-Timing", resp_404.headers)

    def test_x_request_id_present_exactly_once(self):
        """5: X-Request-Id is still present exactly once alongside Server-Timing."""
        test_cases = [
            ("GET", "/health/", {}, status.HTTP_200_OK),
            ("POST", "/api/webhooks/chapa/", {"data": b"{}", "content_type": "application/json"}, status.HTTP_401_UNAUTHORIZED),
            ("GET", "/api/payments/", {"HTTP_X_API_KEY": self.test_key}, status.HTTP_200_OK),
            ("GET", "/non-existent-path-404/", {}, status.HTTP_404_NOT_FOUND),
        ]

        for method, endpoint, kwargs, expected_code in test_cases:
            if method == "POST":
                response = self.client.post(endpoint, **kwargs)
            else:
                response = self.client.get(endpoint, **kwargs)
            self.assertEqual(response.status_code, expected_code)

            # X-Request-Id header exists and appears exactly once
            self.assertIn("X-Request-Id", response.headers)
            req_id_headers = [k for k in response.headers.keys() if k.lower() == "x-request-id"]
            self.assertEqual(len(req_id_headers), 1, f"Expected 1 X-Request-Id header on {endpoint}")

            # Also verify Server-Timing appears exactly once
            st_headers = [k for k in response.headers.keys() if k.lower() == "server-timing"]
            self.assertEqual(len(st_headers), 1, f"Expected 1 Server-Timing header on {endpoint}")
