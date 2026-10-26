"""URLconf with API docs routes excluded (for testing API_DOCS_ENABLED=False)."""
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('payments.urls')),
]
