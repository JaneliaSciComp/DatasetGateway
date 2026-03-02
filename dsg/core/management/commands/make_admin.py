"""Create or promote a user to admin, or revoke admin status.

Creates the user if they don't exist. Sets admin=True and prompts for a
password for Django admin console access (if the user lacks one).

Use --no-password to skip the password prompt.
Use --remove to revoke admin status.

This replaces Django's built-in createsuperuser command.
"""

import getpass

from django.core.management.base import BaseCommand

from core.models import User


class Command(BaseCommand):
    help = "Create or promote a user to admin, or revoke admin status"

    def add_arguments(self, parser):
        parser.add_argument("email", help="Email address of the user")
        parser.add_argument(
            "--no-password",
            action="store_true",
            help="Skip setting a password (user won't be able to log into /admin/)",
        )
        parser.add_argument(
            "--remove",
            action="store_true",
            help="Revoke admin status instead of granting it",
        )

    def handle(self, *args, **options):
        email = options["email"]

        if options["remove"]:
            self._remove_admin(email)
            return

        user, created = User.objects.get_or_create(
            email=email,
            defaults={"name": email.split("@")[0], "admin": True},
        )
        if created:
            user.set_unusable_password()
            self.stdout.write(f"Created new admin user: {email}")
        elif user.admin:
            self.stdout.write(f"User {email} is already an admin.")
        else:
            user.admin = True
            self.stdout.write(f"Promoted {email} to admin.")

        # Set a password if the user doesn't have one and --no-password wasn't given
        if not options["no_password"] and not user.has_usable_password():
            self.stdout.write("A password is needed for Django admin console access.")
            password = self._prompt_password()
            user.set_password(password)

        user.save()
        self.stdout.write(self.style.SUCCESS("Done."))

    def _remove_admin(self, email):
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"User not found: {email}"))
            return

        if not user.admin:
            self.stdout.write(f"User {email} is not an admin.")
            return

        user.admin = False
        user.set_unusable_password()
        user.save()
        self.stdout.write(self.style.SUCCESS(f"Revoked admin status from {email}."))

    def _prompt_password(self):
        while True:
            p1 = getpass.getpass("Password: ")
            p2 = getpass.getpass("Password (again): ")
            if p1 != p2:
                self.stderr.write("Passwords do not match. Try again.")
                continue
            if not p1:
                self.stderr.write("Password cannot be blank.")
                continue
            return p1
