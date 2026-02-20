"""Set the default Site domain to localhost:8000 for local development.

Django's django.contrib.sites creates a Site with domain='example.com'
during migrate. This breaks allauth OAuth callbacks out of the box.
This migration updates it so local dev works without manual intervention.

For production, update the Site domain via the Django admin console or:
    python manage.py shell -c "
    from django.contrib.sites.models import Site
    Site.objects.update_or_create(id=1, defaults={'domain': 'auth.example.org', 'name': 'DatasetGate'})
    "
"""

from django.db import migrations


def set_site_domain(apps, schema_editor):
    Site = apps.get_model("sites", "Site")
    Site.objects.update_or_create(
        id=1,
        defaults={"domain": "localhost:8000", "name": "DatasetGate (dev)"},
    )


def revert_site_domain(apps, schema_editor):
    Site = apps.get_model("sites", "Site")
    Site.objects.filter(id=1).update(domain="example.com", name="example.com")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
        ("sites", "0002_alter_domain_unique"),
    ]

    operations = [
        migrations.RunPython(set_site_domain, revert_site_domain),
    ]
