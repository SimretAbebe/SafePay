import os
from django.conf import settings
from django.test import TestCase

from payments.providers.base import ProviderError
from payments.providers.registry import get_provider
from payments.providers.template_provider import TemplateProvider


class ProviderTemplateTests(TestCase):
    def test_template_imports_and_is_not_registered(self):
        """1. The template imports, and it is NOT in the provider registry."""
        provider = TemplateProvider()
        self.assertEqual(provider.name, "your_provider_name")

        with self.assertRaises(ProviderError):
            get_provider("your_provider_name")

        with self.assertRaises(ProviderError):
            get_provider(provider.name)

    def test_methods_raise_not_implemented(self):
        """2. Each of the five methods raises ProviderError with 'not implemented yet'."""
        provider = TemplateProvider()

        # initialize
        with self.assertRaises(ProviderError) as cm:
            provider.initialize(payment=None)
        self.assertIn("initialize not implemented yet", str(cm.exception))

        # verify
        with self.assertRaises(ProviderError) as cm:
            provider.verify("fake_ref")
        self.assertIn("verify not implemented yet", str(cm.exception))

        # cancel
        with self.assertRaises(ProviderError) as cm:
            provider.cancel("fake_ref")
        self.assertIn("cancel not implemented yet", str(cm.exception))

        # parse_webhook
        with self.assertRaises(ProviderError) as cm:
            provider.parse_webhook(headers={}, body=b"{}")
        self.assertIn("parse_webhook not implemented yet", str(cm.exception))

        # map_status
        with self.assertRaises(ProviderError) as cm:
            provider.map_status("any_status")
        self.assertIn("map_status not implemented yet", str(cm.exception))

    def test_class_attributes_are_all_false(self):
        """3. The template's class attributes are all False."""
        self.assertIs(TemplateProvider.supports_cancel, False)
        self.assertIs(TemplateProvider.supports_livemode, False)
        self.assertIs(TemplateProvider.supports_refund, False)

    def test_guide_file_exists_and_contains_keywords(self):
        """4. The guide file exists and contains the words 'verify' and 'legal'."""
        guide_path = os.path.join(settings.BASE_DIR, "docs", "provider-plugin-guide.md")
        self.assertTrue(os.path.isfile(guide_path), f"Guide file not found at {guide_path}")

        with open(guide_path, "r", encoding="utf-8") as f:
            content = f.read().lower()

        self.assertIn("verify", content)
        self.assertIn("legal", content)
