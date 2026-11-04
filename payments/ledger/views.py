import csv
from datetime import datetime, time as time_cls, timezone
from decimal import Decimal
from io import StringIO

from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.utils.dateparse import parse_date, parse_datetime
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response

from payments.permissions import HasAPIKey
from payments.throttling import LedgerExportRateThrottle, PaymentReadRateThrottle
from payments.views import parse_iso_datetime
from .models import LedgerAccount, LedgerEntry
from .serializers import (
    LedgerBalancesResponseSerializer,
    LedgerEntryListItemSerializer,
)

# Prefixes vulnerable to formula injection in spreadsheet software
DANGEROUS_CSV_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_csv_cell(value) -> str:
    """
    Prevent CSV formula injection by prefixing dangerous characters with a single quote.
    """
    s = str(value) if value is not None else ""
    if s.startswith(DANGEROUS_CSV_PREFIXES):
        return f"'{s}"
    return s


class LedgerEntryCursorPagination(CursorPagination):
    page_size = 20
    page_size_query_param = "limit"
    max_page_size = 100
    ordering = ("-created_at", "-id")

    def get_page_size(self, request):
        if self.page_size_query_param in request.query_params:
            raw_limit = request.query_params[self.page_size_query_param]
            try:
                val = int(raw_limit)
            except (ValueError, TypeError):
                raise ValidationError({"detail": f"Invalid limit: '{raw_limit}'. Must be an integer."})
            if val <= 0:
                raise ValidationError({"detail": f"Invalid limit: {val}. Must be greater than 0."})
            if self.max_page_size and val > self.max_page_size:
                return self.max_page_size
            return val
        return self.page_size

    def paginate_queryset(self, queryset, request, view=None):
        try:
            return super().paginate_queryset(queryset, request, view)
        except (NotFound, Exception) as exc:
            if isinstance(exc, ValidationError):
                raise
            raise ValidationError({"detail": "Invalid or tampered cursor."})


@extend_schema(
    summary="Get ledger account balances",
    description=(
        "Returns the current balance for each of the merchant's ledger accounts in the active mode.\n\n"
        "Balances are dynamically computed as `sum(debits) - sum(credits)` via database aggregation "
        "and are never stored. Accounts with no entries are included with a zero balance."
    ),
    responses={
        200: OpenApiResponse(response=LedgerBalancesResponseSerializer, description="Account balances list."),
        400: OpenApiResponse(description="Bad request."),
        403: OpenApiResponse(description="Invalid or missing API key."),
        404: OpenApiResponse(description="Not found."),
        429: OpenApiResponse(description="Rate limit exceeded."),
    },
    tags=["Ledger"],
)
@api_view(["GET"])
@permission_classes([HasAPIKey])
@throttle_classes([PaymentReadRateThrottle])
def ledger_balances(request):
    """Compute and return current balances for all merchant ledger accounts."""
    accounts_qs = (
        LedgerAccount.objects.filter(
            merchant=request.merchant,
            livemode=request.livemode,
        )
        .annotate(
            total_debits=Coalesce(
                Sum("entries__amount", filter=Q(entries__side="debit")),
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            ),
            total_credits=Coalesce(
                Sum("entries__amount", filter=Q(entries__side="credit")),
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=14, decimal_places=2),
            ),
        )
        .order_by("code")
    )

    accounts_data = []
    default_currency = "ETB"
    for acc in accounts_qs:
        balance = acc.total_debits - acc.total_credits
        accounts_data.append({
            "code": acc.code,
            "currency": acc.currency,
            "balance": f"{balance:.2f}",
        })
        default_currency = acc.currency

    return Response(
        {
            "currency": default_currency,
            "accounts": accounts_data,
        },
        status=status.HTTP_200_OK,
    )


@extend_schema(
    summary="List ledger entries",
    description=(
        "Returns a cursor-paginated list of ledger entries for the authenticated merchant.\n\n"
        "Entries are ordered newest first (`-created_at`, `-id`).\n"
        "Filter by `account` code, `side` ('debit' or 'credit'), `payment_id`, and creation date range."
    ),
    parameters=[
        OpenApiParameter("limit", OpenApiTypes.INT, OpenApiParameter.QUERY, description="Page size (1–100, default 20)."),
        OpenApiParameter("cursor", OpenApiTypes.STR, OpenApiParameter.QUERY, description="Opaque pagination cursor."),
        OpenApiParameter("account", OpenApiTypes.STR, OpenApiParameter.QUERY, description="Exact account code (e.g. 'receivable:chapa', 'sales')."),
        OpenApiParameter("side", OpenApiTypes.STR, OpenApiParameter.QUERY, description="Entry side ('debit' or 'credit')."),
        OpenApiParameter("payment_id", OpenApiTypes.INT, OpenApiParameter.QUERY, description="Filter by payment ID."),
        OpenApiParameter("created_after", OpenApiTypes.STR, OpenApiParameter.QUERY, description="ISO 8601 date or datetime lower bound."),
        OpenApiParameter("created_before", OpenApiTypes.STR, OpenApiParameter.QUERY, description="ISO 8601 date or datetime upper bound."),
    ],
    responses={
        200: OpenApiResponse(response=LedgerEntryListItemSerializer(many=True), description="Paginated ledger entries."),
        400: OpenApiResponse(description="Invalid filter parameter or cursor."),
        403: OpenApiResponse(description="Invalid or missing API key."),
        404: OpenApiResponse(description="Not found."),
        429: OpenApiResponse(description="Rate limit exceeded."),
    },
    tags=["Ledger"],
)
@api_view(["GET"])
@permission_classes([HasAPIKey])
@throttle_classes([PaymentReadRateThrottle])
def ledger_entries(request):
    """Cursor-paginated list of merchant ledger entries."""
    queryset = (
        LedgerEntry.objects.filter(
            account__merchant=request.merchant,
            account__livemode=request.livemode,
        )
        .select_related("account", "journal")
    )

    # 1. account code filter
    account_param = request.query_params.get("account")
    if account_param is not None:
        queryset = queryset.filter(account__code=account_param)

    # 2. side filter
    side_param = request.query_params.get("side")
    if side_param is not None:
        if side_param not in ("debit", "credit"):
            return Response(
                {"detail": f"Invalid side: '{side_param}'. Must be 'debit' or 'credit'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = queryset.filter(side=side_param)

    # 3. payment_id filter
    payment_id_param = request.query_params.get("payment_id")
    if payment_id_param is not None:
        try:
            pid = int(payment_id_param)
        except (ValueError, TypeError):
            return Response(
                {"detail": f"Invalid payment_id: '{payment_id_param}'. Must be an integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = queryset.filter(journal__payment_id=pid)

    # 4. created_after and created_before date filters
    created_after_param = request.query_params.get("created_after")
    created_before_param = request.query_params.get("created_before")

    parsed_after = None
    parsed_before = None

    if created_after_param is not None:
        try:
            parsed_after = parse_iso_datetime(created_after_param, is_end_of_day=False)
        except ValueError:
            return Response(
                {"detail": f"Invalid created_after date or datetime: '{created_after_param}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    if created_before_param is not None:
        try:
            parsed_before = parse_iso_datetime(created_before_param, is_end_of_day=True)
        except ValueError:
            return Response(
                {"detail": f"Invalid created_before date or datetime: '{created_before_param}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    if parsed_after is not None and parsed_before is not None:
        if parsed_after > parsed_before:
            return Response(
                {"detail": "created_after cannot be later than created_before."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    if parsed_after is not None:
        queryset = queryset.filter(created_at__gte=parsed_after)
    if parsed_before is not None:
        queryset = queryset.filter(created_at__lte=parsed_before)

    paginator = LedgerEntryCursorPagination()
    page = paginator.paginate_queryset(queryset, request)
    serializer = LedgerEntryListItemSerializer(page, many=True)
    return paginator.get_paginated_response(serializer.data)


def parse_export_date(val: str, is_end_of_day: bool = False):
    """Parse an ISO date string for CSV export, returning (date_obj, datetime_utc)."""
    if not isinstance(val, str) or not val.strip():
        raise ValueError("Empty or invalid date string.")
    val = val.strip()
    d = parse_date(val)
    if d is not None:
        t = time_cls.max.replace(microsecond=999999) if is_end_of_day else time_cls.min
        return d, datetime.combine(d, t, tzinfo=timezone.utc)
    dt = parse_datetime(val)
    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.date(), dt
    raise ValueError(f"Invalid date: {val}")


@extend_schema(
    summary="Export ledger entries as CSV",
    description=(
        "Exports ledger entries within a specified date range (inclusive, UTC) as a CSV file.\n\n"
        "Requirements:\n"
        "- `from` and `to` query parameters are required (ISO dates).\n"
        "- Date range cannot exceed 366 days.\n"
        "- The export is capped at 10,000 entries. Exceeding this returns HTTP 400.\n"
        "- Cells starting with formula characters (`=`, `+`, `-`, `@`, `\\t`, `\\r`) are escaped."
    ),
    parameters=[
        OpenApiParameter("from", OpenApiTypes.DATE, OpenApiParameter.QUERY, required=True, description="Start date (YYYY-MM-DD, inclusive, UTC)."),
        OpenApiParameter("to", OpenApiTypes.DATE, OpenApiParameter.QUERY, required=True, description="End date (YYYY-MM-DD, inclusive, UTC)."),
    ],
    responses={
        (200, "text/csv"): OpenApiResponse(description="CSV export file.", response=OpenApiTypes.BINARY),
        400: OpenApiResponse(description="Missing/invalid dates, reversed range, range > 366 days, or > 10,000 rows."),
        403: OpenApiResponse(description="Invalid or missing API key."),
        404: OpenApiResponse(description="Not found."),
        429: OpenApiResponse(description="Rate limit exceeded."),
    },
    tags=["Ledger"],
)
@api_view(["GET"])
@permission_classes([HasAPIKey])
@throttle_classes([LedgerExportRateThrottle])
def ledger_export(request):
    """Stream or generate a sanitized CSV export of ledger entries."""
    from_param = request.query_params.get("from")
    to_param = request.query_params.get("to")

    if not from_param or not to_param:
        return Response(
            {"detail": "'from' and 'to' query parameters are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        from_date, from_dt = parse_export_date(from_param, is_end_of_day=False)
    except ValueError:
        return Response(
            {"detail": f"Invalid 'from' date: '{from_param}'. Must be an ISO date (YYYY-MM-DD)."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        to_date, to_dt = parse_export_date(to_param, is_end_of_day=True)
    except ValueError:
        return Response(
            {"detail": f"Invalid 'to' date: '{to_param}'. Must be an ISO date (YYYY-MM-DD)."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if from_date > to_date:
        return Response(
            {"detail": "'from' date cannot be later than 'to' date."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if (to_date - from_date).days > 366:
        return Response(
            {"detail": "Date range cannot exceed 366 days."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    queryset = (
        LedgerEntry.objects.filter(
            account__merchant=request.merchant,
            account__livemode=request.livemode,
            created_at__gte=from_dt,
            created_at__lte=to_dt,
        )
        .select_related("account", "journal")
        .order_by("created_at", "id")
    )

    count = queryset.count()
    if count > 10000:
        return Response(
            {"detail": f"Export contains {count} entries, exceeding the maximum of 10,000 rows. Please select a smaller date range."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    csv_buffer = StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow([
        "created_at",
        "account",
        "side",
        "amount",
        "currency",
        "journal_kind",
        "payment_id",
    ])

    for entry in queryset:
        writer.writerow([
            sanitize_csv_cell(entry.created_at.isoformat()),
            sanitize_csv_cell(entry.account.code),
            sanitize_csv_cell(entry.side),
            sanitize_csv_cell(f"{entry.amount:.2f}"),
            sanitize_csv_cell(entry.account.currency),
            sanitize_csv_cell(entry.journal.kind),
            sanitize_csv_cell(str(entry.journal.payment_id)),
        ])

    from_str = from_date.isoformat()
    to_str = to_date.isoformat()
    filename = f"safepay-ledger-{from_str}-{to_str}.csv"

    response = HttpResponse(
        csv_buffer.getvalue(),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response

