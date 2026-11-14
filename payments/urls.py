from django.urls import path

from . import views
from .ledger import views as ledger_views

urlpatterns = [
    path("payments/", views.payments_dispatch, name="create_payment"),
    path("payments/", views.payments_dispatch, name="create-payment"),
    path("payments/", views.payments_dispatch, name="payment_list"),
    path("payments/<int:pk>/", views.payment_detail, name="payment_detail"),
    path("payments/<int:pk>/investigate/", views.payment_investigate, name="payment_investigate"),
    path("payments/<int:pk>/verify/", views.verify_payment, name="verify_payment"),
    path("payments/<int:pk>/cancel/", views.cancel_payment, name="cancel_payment"),
    path("payments/<int:pk>/refunds/", views.refunds_dispatch, name="create_refund"),
    path("payments/<int:pk>/refunds/", views.refunds_dispatch, name="list_refunds"),
    path("checkout/sessions/", views.create_checkout_session, name="create_checkout_session"),
    path("webhooks/chapa/", views.chapa_webhook, name="chapa_webhook"),
    path("webhooks/arifpay/", views.arifpay_webhook, name="arifpay_webhook"),
    # Webhook delivery log
    path("webhook-deliveries/", views.list_webhook_deliveries, name="webhook_delivery_list"),
    path("webhook-deliveries/<int:pk>/", views.webhook_delivery_detail, name="webhook_delivery_detail"),
    path("webhook-deliveries/<int:pk>/resend/", views.webhook_delivery_resend, name="webhook_delivery_resend"),
    # Ledger read-only endpoints
    path("ledger/balances/", ledger_views.ledger_balances, name="ledger_balances"),
    path("ledger/entries/", ledger_views.ledger_entries, name="ledger_entries"),
    path("ledger/export.csv", ledger_views.ledger_export, name="ledger_export"),
    # Providers read-only endpoint
    path("providers/", views.list_providers, name="list_providers"),
]
