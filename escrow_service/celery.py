from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "escrow_service.settings")

app = Celery("escrow_service")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Static beat schedule (alternative to django-celery-beat admin)
# This will run the expiration task every minute.
app.conf.beat_schedule = {
    "expire-funded-escrows-every-minute": {
        "task": "escrow.tasks.expire_funded_escrows",
        "schedule": 60.0,  # or use crontab(minute="*") for per-minute
    },
}
