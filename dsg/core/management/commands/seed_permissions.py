"""Create the default permission types."""

from django.core.management.base import BaseCommand

from core.models import Permission


class Command(BaseCommand):
    help = "Seed the default permission types (view, edit, manage, admin)"

    def handle(self, *args, **options):
        for name in ("view", "edit", "manage", "admin"):
            obj, created = Permission.objects.get_or_create(name=name)
            if created:
                self.stdout.write(self.style.SUCCESS(f"Created permission: {name}"))
            else:
                self.stdout.write(f"Permission already exists: {name}")
