"""Create default groups."""

from django.core.management.base import BaseCommand

from core.models import Group


class Command(BaseCommand):
    help = "Seed default groups"

    def add_arguments(self, parser):
        parser.add_argument(
            "names", nargs="*", default=["admin", "sc", "team_lead", "user"],
            help="Group names to create (default: admin, sc, team_lead, user)",
        )

    def handle(self, *args, **options):
        for name in options["names"]:
            obj, created = Group.objects.get_or_create(name=name)
            if created:
                self.stdout.write(self.style.SUCCESS(f"Created group: {name}"))
            else:
                self.stdout.write(f"Group already exists: {name}")
