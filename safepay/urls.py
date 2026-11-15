"""
URL configuration for safepay project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
"""
from django.conf import settings
from django.contrib import admin
from django.urls import path, include

from payments.health import liveness_check, readiness_check
from payments.simulator import simulator_dashboard, simulator_action

urlpatterns = [
    path('admin/', admin.site.urls),
    path('health/', liveness_check, name='health_check'),
    path('ready/', readiness_check, name='readiness_check'),
    path('simulator/', simulator_dashboard, name='simulator_dashboard'),
    path('simulator/new/<str:action>/', simulator_action, name='simulator_action'),
    path('api/', include('payments.urls')),
]

if getattr(settings, "API_DOCS_ENABLED", False):
    from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

    urlpatterns += [
        path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
        path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    ]
