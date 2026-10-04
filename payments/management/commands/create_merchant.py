from django.core.management.base import BaseCommand
from payments.models import Merchant


class Command(BaseCommand):
    help = "Creates a new merchant and prints the raw API key once."

    def add_arguments(self, parser):
        parser.add_argument(
            "--name",
            type=str,
            required=True,
            help="Name of the merchant",
        )

    def handle(self, *args, **options):
        name = options["name"]
        merchant, raw_key = Merchant.create_with_key(name=name)
        self.stdout.write(
            self.style.SUCCESS(
                f"Merchant '{merchant.name}' (ID: {merchant.id}) created successfully.\n"
                f"API Key: {raw_key}\n"
                "Save this key in a safe place. It will never be displayed again."
            )
        )
