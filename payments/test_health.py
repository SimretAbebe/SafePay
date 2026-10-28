import time
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from drf_spectacular.generators import SchemaGenerator
from payments.tasks import reconcile_pending_payments


class HealthAndReadinessTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def tearDown(self):
        cache.clear()

    def test_liveness_returns_200_no_auth_zero_queries(self):
        with self.assertNumQueries(0):
            response = self.client.get("/health/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_liveness_post_returns_405(self):
        response = self.client.post("/health/", {})
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    @patch("payments.health._check_broker", return_value="ok")
    @patch("payments.health._check_cache", return_value="ok")
    @patch("payments.health._check_database", return_value="ok")
    def test_readiness_all_healthy_returns_200(self, mock_db, mock_cache, mock_broker):
        response = self.client.get("/ready/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["status"], "ready")
        self.assertEqual(
            data["checks"],
            {"database": "ok", "cache": "ok", "broker": "ok"},
        )
        self.assertIn("jobs", data)
        self.assertIn("reconcile_sweep", data["jobs"])

    def _assert_not_ready_body(self, response, failed_check):
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        data = response.json()
        self.assertEqual(set(data.keys()), {"status", "checks"})
        self.assertEqual(data["status"], "not_ready")
        self.assertEqual(set(data["checks"].keys()), {"database", "cache", "broker"})

        for check_name, check_val in data["checks"].items():
            if check_name == failed_check:
                self.assertEqual(check_val, "fail")
            else:
                self.assertEqual(check_val, "ok")

        content = response.content.decode("utf-8")
        self.assertNotIn("Traceback", content)
        self.assertNotIn("Exception", content)
        self.assertNotIn("localhost", content)
        self.assertNotIn("127.0.0.1", content)
        self.assertNotIn("5432", content)
        self.assertNotIn("6379", content)

    @override_settings(HEALTH_CACHE_SECONDS=0)
    @patch("payments.health._check_broker", return_value="ok")
    @patch("payments.health._check_cache", return_value="ok")
    @patch("payments.health._check_database", side_effect=Exception("DB connection refused"))
    def test_readiness_database_raising_returns_503(self, mock_db, mock_cache, mock_broker):
        response = self.client.get("/ready/")
        self._assert_not_ready_body(response, "database")

    @override_settings(HEALTH_CACHE_SECONDS=0, HEALTH_CHECK_TIMEOUT_SECONDS=0.1)
    @patch("payments.health._check_broker", return_value="ok")
    @patch("payments.health._check_cache", return_value="ok")
    def test_readiness_database_hanging_returns_503(self, mock_cache, mock_broker):
        def _hang():
            time.sleep(2)
            return "ok"

        with patch("payments.health._check_database", side_effect=_hang):
            response = self.client.get("/ready/")
        self._assert_not_ready_body(response, "database")

    @override_settings(HEALTH_CACHE_SECONDS=0)
    @patch("payments.health._check_broker", return_value="ok")
    @patch("payments.health._check_cache", side_effect=RuntimeError("Redis down"))
    @patch("payments.health._check_database", return_value="ok")
    def test_readiness_cache_raising_returns_503(self, mock_db, mock_cache, mock_broker):
        response = self.client.get("/ready/")
        self._assert_not_ready_body(response, "cache")

    @override_settings(HEALTH_CACHE_SECONDS=0, HEALTH_CHECK_TIMEOUT_SECONDS=0.1)
    @patch("payments.health._check_broker", return_value="ok")
    @patch("payments.health._check_database", return_value="ok")
    def test_readiness_cache_hanging_returns_503(self, mock_db, mock_broker):
        def _hang():
            time.sleep(2)
            return "ok"

        with patch("payments.health._check_cache", side_effect=_hang):
            response = self.client.get("/ready/")
        self._assert_not_ready_body(response, "cache")

    @override_settings(HEALTH_CACHE_SECONDS=0)
    @patch("payments.health._check_broker", side_effect=ConnectionError("Broker unreachable"))
    @patch("payments.health._check_cache", return_value="ok")
    @patch("payments.health._check_database", return_value="ok")
    def test_readiness_broker_raising_returns_503(self, mock_db, mock_cache, mock_broker):
        response = self.client.get("/ready/")
        self._assert_not_ready_body(response, "broker")

    @override_settings(HEALTH_CACHE_SECONDS=0, HEALTH_CHECK_TIMEOUT_SECONDS=0.1)
    @patch("payments.health._check_cache", return_value="ok")
    @patch("payments.health._check_database", return_value="ok")
    def test_readiness_broker_hanging_returns_503(self, mock_db, mock_cache):
        def _hang():
            time.sleep(2)
            return "ok"

        with patch("payments.health._check_broker", side_effect=_hang):
            response = self.client.get("/ready/")
        self._assert_not_ready_body(response, "broker")

    @override_settings(HEALTH_CACHE_SECONDS=0, HEALTH_RECONCILE_STALE_SECONDS=300)
    @patch("payments.health._check_broker", return_value="ok")
    @patch("payments.health._check_cache", return_value="ok")
    @patch("payments.health._check_database", return_value="ok")
    def test_reconcile_sweep_freshness_states(self, mock_db, mock_cache, mock_broker):
        cache.delete("safepay:health:reconcile_sweep_ts")
        res_unknown = self.client.get("/ready/")
        self.assertEqual(res_unknown.status_code, status.HTTP_200_OK)
        self.assertEqual(res_unknown.json()["jobs"]["reconcile_sweep"], "unknown")

        with patch("payments.tasks.Payment.objects.select_for_update") as mock_sfu:
            mock_qs = MagicMock()
            mock_qs.filter.return_value.order_by.return_value.__getitem__.return_value = []
            mock_sfu.return_value = mock_qs
            reconcile_pending_payments()

        res_ok = self.client.get("/ready/")
        self.assertEqual(res_ok.status_code, status.HTTP_200_OK)
        self.assertEqual(res_ok.json()["jobs"]["reconcile_sweep"], "ok")

        cache.set("safepay:health:reconcile_sweep_ts", time.time() - 400, timeout=86400)
        res_stale = self.client.get("/ready/")
        self.assertEqual(res_stale.status_code, status.HTTP_200_OK)
        self.assertEqual(res_stale.json()["jobs"]["reconcile_sweep"], "stale")

    @override_settings(HEALTH_CACHE_SECONDS=5)
    @patch("payments.health._check_broker", return_value="ok")
    @patch("payments.health._check_cache", return_value="ok")
    @patch("payments.health._check_database", return_value="ok")
    def test_readiness_caching_window(self, mock_db, mock_cache, mock_broker):
        res1 = self.client.get("/ready/")
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_db.call_count, 1)

        res2 = self.client.get("/ready/")
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_db.call_count, 1)

        cache.delete("safepay:health:ready")
        res3 = self.client.get("/ready/")
        self.assertEqual(res3.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_db.call_count, 2)

    @override_settings(HEALTH_TOKEN="secret-health-token-12345", HEALTH_CACHE_SECONDS=0)
    @patch("payments.health._check_database", return_value="ok")
    @patch("payments.health._check_cache", return_value="ok")
    @patch("payments.health._check_broker", return_value="ok")
    def test_health_token_gating(self, mock_broker, mock_cache, mock_db):
        res_no_tok = self.client.get("/ready/")
        self.assertEqual(res_no_tok.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_no_tok.json(), {"detail": "Forbidden"})
        self.assertEqual(mock_db.call_count, 0)

        res_bad_tok = self.client.get("/ready/", HTTP_X_HEALTH_TOKEN="wrong-token")
        self.assertEqual(res_bad_tok.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(res_bad_tok.json(), {"detail": "Forbidden"})
        self.assertEqual(mock_db.call_count, 0)

        res_ok_tok = self.client.get(
            "/ready/", HTTP_X_HEALTH_TOKEN="secret-health-token-12345"
        )
        self.assertEqual(res_ok_tok.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_db.call_count, 1)

        res_health = self.client.get("/health/")
        self.assertEqual(res_health.status_code, status.HTTP_200_OK)

    @override_settings(HEALTH_TOKEN="", HEALTH_CACHE_SECONDS=0)
    @patch("payments.health._check_broker", return_value="ok")
    @patch("payments.health._check_cache", return_value="ok")
    @patch("payments.health._check_database", return_value="ok")
    def test_empty_health_token_needs_no_auth(self, mock_db, mock_cache, mock_broker):
        response = self.client.get("/ready/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_settings(HEALTH_THROTTLE_RATE="2/minute", HEALTH_CACHE_SECONDS=0)
    def test_health_throttle_returns_429_beyond_rate(self):
        cache.clear()

        res1 = self.client.get("/health/")
        self.assertEqual(res1.status_code, status.HTTP_200_OK)

        res2 = self.client.get("/health/")
        self.assertEqual(res2.status_code, status.HTTP_200_OK)

        res3 = self.client.get("/health/")
        self.assertEqual(res3.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_openapi_schema_excludes_health_and_ready(self):
        generator = SchemaGenerator()
        schema = generator.get_schema(request=None, public=True)
        paths = schema.get("paths", {})

        self.assertNotIn("/health/", paths)
        self.assertNotIn("/ready/", paths)
