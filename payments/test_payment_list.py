from datetime import datetime, timedelta, timezone
from decimal import Decimal
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from payments.models import Merchant, Payment
from payments.providers import register_provider
from payments.providers.fake import FakeProvider
from payments.test_utils import create_test_merchant


class LiveDouble(FakeProvider):
    name = "live_double"
    supports_livemode = True


class PaymentListTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.merchant_a, self.key_a = create_test_merchant(
            name="Shop A",
            live_enabled=True,
            default_provider="live_double",
        )
        self.merchant_b, self.key_b = create_test_merchant(
            name="Shop B",
            live_enabled=False,
        )
        self.client.credentials(HTTP_X_API_KEY=self.key_a)
        self.url = reverse("payment_list")

    def tearDown(self):
        cache.clear()

    def _create_payment(self, merchant, livemode=False, **kwargs):
        defaults = {
            "amount": Decimal("100.00"),
            "sender": "customer@example.com",
            "receiver": merchant.name,
            "status": "pending",
            "idempotency_key": f"key-{Payment.objects.count() + 1}",
        }
        defaults.update(kwargs)
        return Payment.objects.create(merchant=merchant, livemode=livemode, **defaults)

    # 1. The list returns only the authenticated merchant's payments
    def test_merchant_isolation(self):
        p_a1 = self._create_payment(self.merchant_a, idempotency_key="a1")
        p_a2 = self._create_payment(self.merchant_a, idempotency_key="a2")
        p_b = self._create_payment(self.merchant_b, idempotency_key="b1")

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        returned_ids = [p["id"] for p in response.data["results"]]
        self.assertIn(p_a1.id, returned_ids)
        self.assertIn(p_a2.id, returned_ids)
        self.assertNotIn(p_b.id, returned_ids)

    # 2. Mode separation: test key lists only livemode False; live key lists only livemode True
    @override_settings(LIVE_MODE_AVAILABLE=True)
    def test_mode_separation(self):
        register_provider(LiveDouble())
        live_key_a = self.merchant_a.create_live_key()

        p_test = self._create_payment(self.merchant_a, livemode=False, idempotency_key="test-1")
        p_live = self._create_payment(self.merchant_a, livemode=True, idempotency_key="live-1")

        # Query with test key
        res_test = self.client.get(self.url)
        self.assertEqual(res_test.status_code, status.HTTP_200_OK)
        ids_test = [p["id"] for p in res_test.data["results"]]
        self.assertIn(p_test.id, ids_test)
        self.assertNotIn(p_live.id, ids_test)

        # Query with live key
        client_live = APIClient()
        client_live.credentials(HTTP_X_API_KEY=live_key_a)
        res_live = client_live.get(self.url)
        self.assertEqual(res_live.status_code, status.HTTP_200_OK)
        ids_live = [p["id"] for p in res_live.data["results"]]
        self.assertIn(p_live.id, ids_live)
        self.assertNotIn(p_test.id, ids_live)

    # 3. Newest first; two payments with identical created_at are ordered by id, stable across requests
    def test_ordering_and_stable_tie_break(self):
        base_time = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        p1 = self._create_payment(self.merchant_a, idempotency_key="ord-1")
        p2 = self._create_payment(self.merchant_a, idempotency_key="ord-2")
        p3 = self._create_payment(self.merchant_a, idempotency_key="ord-3")

        Payment.objects.filter(id=p1.id).update(created_at=base_time)
        Payment.objects.filter(id=p2.id).update(created_at=base_time + timedelta(hours=1))
        # p3 has identical created_at to p2
        Payment.objects.filter(id=p3.id).update(created_at=base_time + timedelta(hours=1))

        # Expected order: newest created_at first. Between p2 and p3 (same created_at), higher id first
        higher_id = max(p2.id, p3.id)
        lower_id = min(p2.id, p3.id)
        expected_ids = [higher_id, lower_id, p1.id]

        for _ in range(3):
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            actual_ids = [p["id"] for p in response.data["results"]]
            self.assertEqual(actual_ids, expected_ids)

    # 4. Status filter: valid status works; invalid status returns 400
    def test_status_filter(self):
        p_pending = self._create_payment(self.merchant_a, status="pending", idempotency_key="s-1")
        p_succeeded = self._create_payment(self.merchant_a, status="succeeded", idempotency_key="s-2")

        # Valid status
        res_valid = self.client.get(self.url, {"status": "succeeded"})
        self.assertEqual(res_valid.status_code, status.HTTP_200_OK)
        ids = [p["id"] for p in res_valid.data["results"]]
        self.assertEqual(ids, [p_succeeded.id])

        # Invalid status -> 400
        res_invalid = self.client.get(self.url, {"status": "invalid_status"})
        self.assertEqual(res_invalid.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", res_invalid.data)

    # 5. provider and reference filters work and combine with status (AND)
    def test_provider_and_reference_filters_with_status_and(self):
        p1 = self._create_payment(
            self.merchant_a,
            provider="chapa",
            merchant_reference="ref-100",
            status="pending",
            idempotency_key="pr-1",
        )
        p2 = self._create_payment(
            self.merchant_a,
            provider="chapa",
            merchant_reference="ref-100",
            status="succeeded",
            idempotency_key="pr-2",
        )
        p3 = self._create_payment(
            self.merchant_a,
            provider="arifpay",
            merchant_reference="ref-100",
            status="succeeded",
            idempotency_key="pr-3",
        )

        # Provider filter
        res = self.client.get(self.url, {"provider": "chapa"})
        ids = [p["id"] for p in res.data["results"]]
        self.assertCountEqual(ids, [p1.id, p2.id])

        # Reference filter
        res = self.client.get(self.url, {"reference": "ref-100"})
        ids = [p["id"] for p in res.data["results"]]
        self.assertCountEqual(ids, [p1.id, p2.id, p3.id])

        # Combined AND
        res = self.client.get(
            self.url,
            {"provider": "chapa", "reference": "ref-100", "status": "succeeded"},
        )
        ids = [p["id"] for p in res.data["results"]]
        self.assertEqual(ids, [p2.id])

        # Provider with no matches -> returns empty results with 200 (no error)
        res_empty = self.client.get(self.url, {"provider": "nonexistent"})
        self.assertEqual(res_empty.status_code, status.HTTP_200_OK)
        self.assertEqual(res_empty.data["results"], [])

    # 6. created_after / created_before: date-only and datetime, inclusive bounds, reversed range -> 400, invalid -> 400
    def test_created_after_and_before_filters(self):
        t1 = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)

        p1 = self._create_payment(self.merchant_a, idempotency_key="d-1")
        p2 = self._create_payment(self.merchant_a, idempotency_key="d-2")
        p3 = self._create_payment(self.merchant_a, idempotency_key="d-3")

        Payment.objects.filter(id=p1.id).update(created_at=t1)
        Payment.objects.filter(id=p2.id).update(created_at=t2)
        Payment.objects.filter(id=p3.id).update(created_at=t3)

        # Date-only created_after (start of day UTC)
        res = self.client.get(self.url, {"created_after": "2026-05-02"})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [p["id"] for p in res.data["results"]]
        self.assertCountEqual(ids, [p2.id, p3.id])

        # Date-only created_before (end of day UTC)
        res = self.client.get(self.url, {"created_before": "2026-05-02"})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [p["id"] for p in res.data["results"]]
        self.assertCountEqual(ids, [p1.id, p2.id])

        # Datetime created_after and created_before inclusive bounds
        res = self.client.get(
            self.url,
            {
                "created_after": "2026-05-02T12:00:00Z",
                "created_before": "2026-05-02T12:00:00Z",
            },
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        ids = [p["id"] for p in res.data["results"]]
        self.assertEqual(ids, [p2.id])

        # Reversed range -> 400
        res_rev = self.client.get(
            self.url,
            {"created_after": "2026-05-03", "created_before": "2026-05-01"},
        )
        self.assertEqual(res_rev.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", res_rev.data)

        # Invalid date -> 400
        res_bad = self.client.get(self.url, {"created_after": "not-a-date"})
        self.assertEqual(res_bad.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", res_bad.data)

    # 7. Pagination: limit=3 over 8 payments gives pages of 3, 3, 2 with correct next/previous links, no dups/gaps
    def test_cursor_pagination_pages(self):
        t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        payments = []
        for i in range(8):
            p = self._create_payment(self.merchant_a, idempotency_key=f"pg-{i}")
            Payment.objects.filter(id=p.id).update(created_at=t0 + timedelta(hours=i))
            payments.append(p)

        # Page 1 (limit=3)
        res1 = self.client.get(self.url, {"limit": "3"})
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res1.data["results"]), 3)
        self.assertIsNotNone(res1.data["next"])
        self.assertIsNone(res1.data["previous"])
        page1_ids = [p["id"] for p in res1.data["results"]]

        # Page 2
        res2 = self.client.get(res1.data["next"])
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res2.data["results"]), 3)
        self.assertIsNotNone(res2.data["next"])
        self.assertIsNotNone(res2.data["previous"])
        page2_ids = [p["id"] for p in res2.data["results"]]

        # Page 3
        res3 = self.client.get(res2.data["next"])
        self.assertEqual(res3.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res3.data["results"]), 2)
        self.assertIsNone(res3.data["next"])
        self.assertIsNotNone(res3.data["previous"])
        page3_ids = [p["id"] for p in res3.data["results"]]

        # Verify no duplicates and no gaps
        all_ids = page1_ids + page2_ids + page3_ids
        self.assertEqual(len(all_ids), 8)
        self.assertEqual(len(set(all_ids)), 8)

        expected_order = list(
            Payment.objects.filter(merchant=self.merchant_a)
            .order_by("-created_at", "-id")
            .values_list("id", flat=True)
        )
        self.assertEqual(all_ids, expected_order)

    # 8. Cursor stability: create a new payment between page 1 and following "next"; page 2 has no dup and skips none
    def test_cursor_stability_with_concurrent_inserts(self):
        t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        for i in range(8):
            p = self._create_payment(self.merchant_a, idempotency_key=f"stab-{i}")
            Payment.objects.filter(id=p.id).update(created_at=t0 + timedelta(hours=i))

        # Expected initial order
        expected_all = list(
            Payment.objects.filter(merchant=self.merchant_a)
            .order_by("-created_at", "-id")
            .values_list("id", flat=True)
        )

        res1 = self.client.get(self.url, {"limit": "3"})
        page1_ids = [p["id"] for p in res1.data["results"]]
        self.assertEqual(page1_ids, expected_all[:3])
        next_url = res1.data["next"]

        # Insert a new payment AFTER page 1 was retrieved (with newer created_at)
        p_new = self._create_payment(self.merchant_a, idempotency_key="stab-new")
        Payment.objects.filter(id=p_new.id).update(created_at=t0 + timedelta(hours=20))

        # Follow next
        res2 = self.client.get(next_url)
        page2_ids = [p["id"] for p in res2.data["results"]]

        # Page 2 must not contain any item from page 1, and must match expected_all[3:6]
        self.assertEqual(page2_ids, expected_all[3:6])
        for pid in page1_ids:
            self.assertNotIn(pid, page2_ids)

    # 9. limit above 100 clamped; limit=0, limit=-1, limit=abc -> 400; tampered cursor -> 400
    def test_limit_validation_and_tampered_cursor(self):
        # limit=0 -> 400
        res = self.client.get(self.url, {"limit": "0"})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", res.data)

        # limit=-1 -> 400
        res = self.client.get(self.url, {"limit": "-1"})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", res.data)

        # limit=abc -> 400
        res = self.client.get(self.url, {"limit": "abc"})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", res.data)

        # limit above 100 is clamped to 100
        for i in range(105):
            self._create_payment(self.merchant_a, idempotency_key=f"clamp-{i}")

        res = self.client.get(self.url, {"limit": "150"})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data["results"]), 100)

        # Tampered cursor -> 400
        res_tampered = self.client.get(self.url, {"cursor": "invalid_cursor_xyz123"})
        self.assertEqual(res_tampered.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("detail", res_tampered.data)

    # 10. Key set assertion: no count, exact allowed keys, no internal/provider fields
    def test_result_fields_exact_allowlist(self):
        p = self._create_payment(
            self.merchant_a,
            amount=Decimal("45.50"),
            provider="fake",
            provider_reference="prov-ref-123",
            provider_status="paid",
            merchant_reference="order-999",
            checkout_url="https://checkout.example.com/pay",
            sender="customer@test.com",
            receiver="Shop A",
        )

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("count", response.data)
        self.assertIn("next", response.data)
        self.assertIn("previous", response.data)
        self.assertIn("results", response.data)

        item = response.data["results"][0]
        expected_keys = {
            "id",
            "status",
            "amount",
            "provider",
            "reference",
            "checkout_url",
            "livemode",
            "created_at",
            "updated_at",
        }
        self.assertEqual(set(item.keys()), expected_keys)
        self.assertEqual(item["amount"], "45.50")
        self.assertEqual(item["reference"], "order-999")
        self.assertEqual(item["checkout_url"], "https://checkout.example.com/pay")

        # Confirm excluded fields are absent
        for forbidden in ["provider_reference", "provider_status", "idempotency_key", "sender", "receiver"]:
            self.assertNotIn(forbidden, item)

    # 11. Missing or wrong API key -> 403; unknown query parameters ignored
    def test_auth_and_unknown_query_parameters(self):
        # Missing API key -> 403
        client_no_auth = APIClient()
        res_no_auth = client_no_auth.get(self.url)
        self.assertEqual(res_no_auth.status_code, status.HTTP_403_FORBIDDEN)

        # Wrong API key -> 403
        client_wrong_auth = APIClient()
        client_wrong_auth.credentials(HTTP_X_API_KEY="sp_test_invalid_key_12345")
        res_wrong = client_wrong_auth.get(self.url)
        self.assertEqual(res_wrong.status_code, status.HTTP_403_FORBIDDEN)

        # Unknown query parameters are ignored
        self._create_payment(self.merchant_a, idempotency_key="ign-1")
        res_unknown = self.client.get(self.url, {"unknown_param": "value", "another": "123"})
        self.assertEqual(res_unknown.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res_unknown.data["results"]), 1)

    # 12. Reads throttle has its own bucket: exhausting it does not throttle POST /api/payments/
    def test_reads_throttle_separate_bucket(self):
        cache.clear()
        with override_settings(PAYMENT_READ_THROTTLE_RATE="2/minute"):
            # First 2 reads pass
            res1 = self.client.get(self.url)
            self.assertEqual(res1.status_code, status.HTTP_200_OK)
            res2 = self.client.get(self.url)
            self.assertEqual(res2.status_code, status.HTTP_200_OK)

            # 3rd read throttled -> 429
            res3 = self.client.get(self.url)
            self.assertEqual(res3.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

            # POST /api/payments/ still succeeds (not throttled by reads bucket)
            post_payload = {
                "amount": "50.00",
                "sender": "alice",
                "receiver": "bob",
                "idempotency_key": "separate-throttle-post-key",
            }
            post_res = self.client.post(self.url, post_payload, format="json")
            self.assertEqual(post_res.status_code, status.HTTP_201_CREATED)

    # 13. Database query count is constant regardless of page size
    def test_constant_database_queries(self):
        for i in range(25):
            self._create_payment(self.merchant_a, idempotency_key=f"qc-{i}")

        # Page of 2
        with CaptureQueriesContext(connection) as ctx2:
            res2 = self.client.get(self.url, {"limit": "2"})
            self.assertEqual(res2.status_code, status.HTTP_200_OK)

        # Page of 20
        with CaptureQueriesContext(connection) as ctx20:
            res20 = self.client.get(self.url, {"limit": "20"})
            self.assertEqual(res20.status_code, status.HTTP_200_OK)

        self.assertEqual(
            len(ctx2.captured_queries),
            len(ctx20.captured_queries),
            f"Query counts differ: limit=2 did {len(ctx2.captured_queries)}, limit=20 did {len(ctx20.captured_queries)}"
        )

    # 14. An empty list returns 200 with "results": []
    def test_empty_list_returns_200(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["results"], [])
        self.assertIsNone(response.data["next"])
        self.assertIsNone(response.data["previous"])

    # 15. POST /api/payments/ still behaves as before and reverse("create_payment") works
    def test_post_create_payment_backwards_compatibility(self):
        self.assertEqual(reverse("create_payment"), "/api/payments/")
        self.assertEqual(reverse("create-payment"), "/api/payments/")

        payload = {
            "amount": "75.00",
            "sender": "payer",
            "receiver": "receiver",
            "idempotency_key": "compat-idem-key-1",
        }
        res = self.client.post(reverse("create_payment"), payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["amount"], "75.00")
        self.assertEqual(res.data["idempotency_key"], "compat-idem-key-1")

        # Same idempotency key returns 200 with same payment
        res_idem = self.client.post(reverse("create_payment"), payload, format="json")
        self.assertEqual(res_idem.status_code, status.HTTP_200_OK)
        self.assertEqual(res_idem.data["id"], res.data["id"])

        # Method other than GET and POST returns 405
        res_put = self.client.put(self.url, payload, format="json")
        self.assertEqual(res_put.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
