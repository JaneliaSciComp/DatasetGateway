"""Promote or demote a user's admin status.

Sets admin=True and optionally sets a password for Django admin console
access. If the user has no usable password, prompts for one automatically.
Use --no-password to skip the password prompt.
Use --remove to revoke admin status.
"""

import getpass

from django.core.management.base import BaseCommand, CommandError

from core.models import User


class Command(BaseCommand):
    help = "Promote or demote a user's admin status"

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
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            raise CommandError(
                f"User not found: {email}\n"
                "The user must exist first (via OAuth login or an import command)."
            )

        if options["remove"]:
            if not user.admin:
                self.stdout.write(f"User {email} is not an admin.")
                return
            user.admin = False
            user.set_unusable_password()
            user.save()
            self.stdout.write(self.style.SUCCESS(f"Revoked admin status from {email}."))
            return

        changed = False

        if user.admin:
            self.stdout.write(f"User {email} is already an admin.")
        else:
            user.admin = True
            changed = True

        # Set a password if the user doesn't have one and --no-password wasn't given
        if not options["no_password"] and not user.has_usable_password():
            self.stdout.write("A password is needed for Django admin console access.")
            password = self._prompt_password()
            user.set_password(password)
            changed = True

        if changed:
            user.save()
            self.stdout.write(self.style.SUCCESS(f"Promoted {email} to admin."))
        else:
            self.stdout.write("No changes needed.")

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
