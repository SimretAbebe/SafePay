import os

from celery import Celery

# Tells Celery which Django settings module to use -- same settings.py
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "safepay.settings")

app = Celery("safepay")

# Reads any setting prefixed with CELERY_ from settings.py automatically
app.config_from_object("django.conf:settings", namespace="CELERY")

app.autodiscover_tasks()