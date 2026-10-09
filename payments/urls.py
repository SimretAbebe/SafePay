from django.urls import path

from . import views

urlpatterns = [
    path("payments/", views.create_payment, name="create-payment"),
    path("payments/<int:pk>/", views.payment_detail, name="payment_detail"),
    path("payments/<int:pk>/verify/", views.verify_payment, name="verify_payment"),
    path("payments/<int:pk>/cancel/", views.cancel_payment, name="cancel_payment"),
    path("checkout/sessions/", views.create_checkout_session, name="create_checkout_session"),
]