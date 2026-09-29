from django.test import TestCase

from .tasks import log_payment_event


class CeleryTaskTests(TestCase):

    def test_log_payment_event_runs_and_returns_expected_result(self):
        result = log_payment_event.delay(1, "test event")
        self.assertEqual(result.get(), "Logged: test event")