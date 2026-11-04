from rest_framework import serializers

from .models import LedgerAccount, LedgerEntry


class LedgerAccountBalanceSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="Account code (e.g. receivable:chapa, sales).")
    currency = serializers.CharField(help_text="Account currency (e.g. ETB).")
    balance = serializers.CharField(help_text="Current balance (sum of debits minus credits) as a decimal string.")


class LedgerBalancesResponseSerializer(serializers.Serializer):
    currency = serializers.CharField(help_text="Reporting currency.")
    accounts = LedgerAccountBalanceSerializer(many=True, help_text="List of accounts and their current balances.")


class LedgerEntryListItemSerializer(serializers.ModelSerializer):
    account = serializers.CharField(source="account.code", help_text="Account code.")
    currency = serializers.CharField(source="account.currency", help_text="Account currency.")
    journal_kind = serializers.CharField(source="journal.kind", help_text="Kind of journal event (e.g. payment_succeeded).")
    payment_id = serializers.IntegerField(source="journal.payment_id", help_text="Associated payment ID.")
    amount = serializers.SerializerMethodField(help_text="Entry amount as a 2-decimal string.")

    class Meta:
        model = LedgerEntry
        fields = [
            "id",
            "account",
            "side",
            "amount",
            "currency",
            "journal_kind",
            "payment_id",
            "created_at",
        ]

    def get_amount(self, obj) -> str:
        return f"{obj.amount:.2f}"

