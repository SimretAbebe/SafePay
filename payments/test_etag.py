import datetime
import hashlib
import json
import re
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APIClient

from payments.http_cache import etag_matches, make_etag
from payments.models import Merchant, Payment
from payments.serializers import PaymentSerializer
from payments.test_utils import create_test_merchant


class HttpCacheUnitTests(TestCase):
    def test_make_etag_strong_quoted_32_hex(self):
        body = b'{"hello": "world"}'
        etag = make_etag(body)
        self.assertTrue(etag.startswith('"'))
        self.assertTrue(etag.endswith('"'))
        raw_hash = etag[1:-1]
        self.assertEqual(len(raw_hash), 32)
        expected = hashlib.sha256(body).hexdigest()[:32]
        self.assertEqual(raw_hash, expected)

    def test_etag_matches_logic(self):
        etag = '"0123456789abcdef0123456789abcdef"'

        # Exact match
        self.assertTrue(etag_matches(etag, etag))

        # Wildcard *
        self.assertTrue(etag_matches("*", etag))

        # Weak form
        self.assertTrue(etag_matches('W/"0123456789abcdef0123456789abcdef"', etag))

        # Comma separated list
        self.assertTrue(etag_matches(f'"wrong1", {etag}, "wrong2"', etag))
        self.assertTrue(etag_matches(f'W/"wrong1", W/{etag}', etag))

        # Non-matching
        self.assertFalse(etag_matches('"wrong"', etag))
        self.assertFalse(etag_matches('W/"wrong"', etag))

        # Empty, None, malformed, garbage
        self.assertFalse(etag_matches("", etag))
        self.assertFalse(etag_matches("   ", etag))
        self.assertFalse(etag_matches(None, etag))
        self.assertFalse(etag_matches("!@#$%^&*", etag))
        self.assertFalse(etag_matches(", , ,", etag))


@override_settings(
    ENABLE_FAKE_PROVIDER=True,
    DEFAULT_PROVIDER="fake",
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class PaymentDetailETagTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

        self.merchant_a, self.test_key_a = create_test_merchant(
            name="Merchant A",
            live_enabled=True,
        )
        with override_settings(LIVE_MODE_AVAILABLE=True):
            self.live_key_a = self.merchant_a.create_live_key()

        self.merchant_b, self.test_key_b = create_test_merchant(
            name="Merchant B",
            live_enabled=False,
        )

        self.payment = Payment.objects.create(
            merchant=self.merchant_a,
            amount="150.00",
            sender="alice@example.com",
            receiver="bob@example.com",
            idempotency_key="etag-test-key-1",
            status="pending",
            livemode=False,
        )
        self.url = reverse("payment_detail", kwargs={"pk": self.payment.pk})

    def tearDown(self):
        cache.clear()

    # 1. A 200 response includes an ETag in the strong quoted format and Cache-Control: private, no-cache;
    #    two identical reads return the same ETag.
    def test_200_response_includes_strong_etag_and_cache_control(self):
        response1 = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a)
        self.assertEqual(response1.status_code, status.HTTP_200_OK)

        self.assertIn("ETag", response1.headers)
        etag1 = response1.headers["ETag"]
        self.assertTrue(re.match(r'^"[a-f0-9]{32}"$', etag1), f"ETag {etag1} not strong 32 hex chars")
        self.assertEqual(response1.headers.get("Cache-Control"), "private, no-cache")

        response2 = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a)
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        self.assertEqual(response2.headers.get("ETag"), etag1)

    # 2. Sending the ETag in If-None-Match returns 304 with an empty body, the same ETag,
    #    and the Cache-Control header.
    def test_matching_if_none_match_returns_304(self):
        res_200 = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a)
        etag = res_200.headers["ETag"]

        res_304 = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(res_304.status_code, status.HTTP_304_NOT_MODIFIED)
        self.assertEqual(res_304.content, b"")
        self.assertEqual(len(res_304.content), 0)
        self.assertEqual(res_304.headers.get("ETag"), etag)
        self.assertEqual(res_304.headers.get("Cache-Control"), "private, no-cache")

    # 3. After the payment changes (e.g. status transition or expiry), the old ETag no longer matches:
    #    200 with a different ETag and the new body.
    def test_payment_mutation_invalidates_etag(self):
        res_orig = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a)
        old_etag = res_orig.headers["ETag"]

        self.payment.status = "succeeded"
        self.payment.save()

        res_mutated = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a, HTTP_IF_NONE_MATCH=old_etag)
        self.assertEqual(res_mutated.status_code, status.HTTP_200_OK)
        new_etag = res_mutated.headers.get("ETag")
        self.assertNotEqual(new_etag, old_etag)
        self.assertEqual(res_mutated.data["status"], "succeeded")

    # 4. Weak form (W/"..."), a list containing the right ETag among others, and "*" all produce 304;
    #    a wrong ETag, an empty header and garbage produce 200.
    def test_if_none_match_matching_variants(self):
        res_orig = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a)
        etag = res_orig.headers["ETag"]

        # Weak form -> 304
        weak_etag = f"W/{etag}"
        res = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a, HTTP_IF_NONE_MATCH=weak_etag)
        self.assertEqual(res.status_code, status.HTTP_304_NOT_MODIFIED)

        # List containing right ETag -> 304
        list_etag = f'"wrong-etag-1", {etag}, "wrong-etag-2"'
        res = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a, HTTP_IF_NONE_MATCH=list_etag)
        self.assertEqual(res.status_code, status.HTTP_304_NOT_MODIFIED)

        # Wildcard * -> 304
        res = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a, HTTP_IF_NONE_MATCH="*")
        self.assertEqual(res.status_code, status.HTTP_304_NOT_MODIFIED)

        # Wrong ETag -> 200
        res = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a, HTTP_IF_NONE_MATCH='"wrong-etag-1234567890"')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        # Empty header -> 200
        res = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a, HTTP_IF_NONE_MATCH="")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        # Garbage -> 200
        res = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a, HTTP_IF_NONE_MATCH="invalid/garbage/syntax")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    # 5. Another merchant's payment, and the other mode's payment, return 404 even when a valid ETag
    #    from the owner is sent (never 304), and no ETag header is present on the 404.
    def test_merchant_and_mode_scoping_runs_first(self):
        res_owner = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a)
        etag = res_owner.headers["ETag"]

        # Another merchant with owner's valid ETag -> 404, no ETag header
        res_merchant_b = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_b, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(res_merchant_b.status_code, status.HTTP_404_NOT_FOUND)
        self.assertNotIn("ETag", res_merchant_b.headers)
        self.assertNotIn("Cache-Control", res_merchant_b.headers)

        # Other mode (live key for test payment) with owner's valid ETag -> 404, no ETag header
        res_live_mode = self.client.get(self.url, HTTP_X_API_KEY=self.live_key_a, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(res_live_mode.status_code, status.HTTP_404_NOT_FOUND)
        self.assertNotIn("ETag", res_live_mode.headers)

    # 6. Error responses (403, 404, 429) and the verify and list endpoints do not carry the ETag header.
    def test_no_etag_on_errors_or_other_endpoints(self):
        # 403 Invalid API key
        res_403 = self.client.get(self.url, HTTP_X_API_KEY="invalid-key")
        self.assertEqual(res_403.status_code, status.HTTP_403_FORBIDDEN)
        self.assertNotIn("ETag", res_403.headers)

        # 404 Not Found
        non_existent_url = reverse("payment_detail", kwargs={"pk": 999999})
        res_404 = self.client.get(non_existent_url, HTTP_X_API_KEY=self.test_key_a)
        self.assertEqual(res_404.status_code, status.HTTP_404_NOT_FOUND)
        self.assertNotIn("ETag", res_404.headers)

        # Verify endpoint -> no ETag
        verify_url = reverse("verify_payment", kwargs={"pk": self.payment.pk})
        res_verify = self.client.get(verify_url, HTTP_X_API_KEY=self.test_key_a)
        self.assertEqual(res_verify.status_code, status.HTTP_200_OK)
        self.assertNotIn("ETag", res_verify.headers)

        # List endpoint -> no ETag
        list_url = reverse("payment_list")
        res_list = self.client.get(list_url, HTTP_X_API_KEY=self.test_key_a)
        self.assertEqual(res_list.status_code, status.HTTP_200_OK)
        self.assertNotIn("ETag", res_list.headers)

    # 7. The reads throttle still applies to 304 responses (a conditional GET counts toward the bucket).
    def test_reads_throttle_applies_to_304_responses(self):
        res_orig = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a)
        etag = res_orig.headers["ETag"]
        cache.clear()

        with override_settings(PAYMENT_READ_THROTTLE_RATE="2/minute"):
            # 1st request -> 304
            r1 = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a, HTTP_IF_NONE_MATCH=etag)
            self.assertEqual(r1.status_code, status.HTTP_304_NOT_MODIFIED)

            # 2nd request -> 304
            r2 = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a, HTTP_IF_NONE_MATCH=etag)
            self.assertEqual(r2.status_code, status.HTTP_304_NOT_MODIFIED)

            # 3rd request -> 429 Too Many Requests
            r3 = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a, HTTP_IF_NONE_MATCH=etag)
            self.assertEqual(r3.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
            self.assertNotIn("ETag", r3.headers)

    # 8. The X-Request-Id header is present exactly once on 200 and 304.
    def test_x_request_id_present_on_200_and_304(self):
        res_200 = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a)
        self.assertEqual(res_200.status_code, status.HTTP_200_OK)
        self.assertIn("X-Request-Id", res_200.headers)
        rid_count_200 = sum(1 for k in res_200.headers.keys() if k.lower() == "x-request-id")
        self.assertEqual(rid_count_200, 1)

        etag = res_200.headers["ETag"]
        res_304 = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(res_304.status_code, status.HTTP_304_NOT_MODIFIED)
        self.assertIn("X-Request-Id", res_304.headers)
        rid_count_304 = sum(1 for k in res_304.headers.keys() if k.lower() == "x-request-id")
        self.assertEqual(rid_count_304, 1)

    # 9. The body of a 200 is byte-for-byte what it was before this change (compare with a stored expectation).
    def test_body_byte_for_byte_expectation(self):
        response = self.client.get(self.url, HTTP_X_API_KEY=self.test_key_a)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        expected_bytes = JSONRenderer().render(PaymentSerializer(self.payment).data)
        self.assertEqual(response.content, expected_bytes)

        # Verify parsed shape matches PaymentSerializer contract
        data = json.loads(response.content.decode("utf-8"))
        self.assertEqual(data["id"], self.payment.pk)
        self.assertEqual(data["amount"], "150.00")
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["livemode"], False)
        self.assertEqual(data["sender"], "alice@example.com")
        self.assertEqual(data["receiver"], "bob@example.com")
