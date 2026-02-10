"""ASGI config for DatasetGate."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "datasetgate.settings")

application = get_asgi_application()
