from django.urls import path

from . import views

urlpatterns = [
    path("payments/", views.create_payment, name="create-payment"),
]