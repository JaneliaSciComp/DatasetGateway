"""Promote a user to admin by email address."""

from django.core.management.base import BaseCommand, CommandError

from core.models import User


class Command(BaseCommand):
    help = "Promote a user to admin (user must have logged in via OAuth first)"

    def add_arguments(self, parser):
        parser.add_argument("email", help="Email address of the user to promote")

    def handle(self, *args, **options):
        email = options["email"]
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            raise CommandError(
                f"User not found: {email}\n"
                "The user must log in via Google OAuth first to create their account."
            )

        if user.admin:
            self.stdout.write(f"User {email} is already an admin.")
            return

        user.admin = True
        user.save(update_fields=["admin"])
        self.stdout.write(self.style.SUCCESS(f"Promoted {email} to admin."))
