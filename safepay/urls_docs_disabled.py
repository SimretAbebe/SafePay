"""URLconf with API docs routes excluded (for testing API_DOCS_ENABLED=False)."""
from django.contrib import admin
from django.urls import path, include

from payments.health import liveness_check, readiness_check

urlpatterns = [
    path('admin/', admin.site.urls),
    path('health/', liveness_check, name='health_check'),
    path('ready/', readiness_check, name='readiness_check'),
    path('api/', include('payments.urls')),
]
