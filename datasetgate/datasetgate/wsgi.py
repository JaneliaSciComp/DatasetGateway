"""WSGI config for DatasetGate."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "datasetgate.settings")

application = get_wsgi_application()
