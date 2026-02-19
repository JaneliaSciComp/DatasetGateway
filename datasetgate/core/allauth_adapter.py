"""Allauth adapters for DatasetGate.

Bridges allauth's login/signup flow with DatasetGate's APIKey token system.
"""

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter


class AccountAdapter(DefaultAccountAdapter):
    """Custom account adapter — creates APIKey on login and handles redirects."""

    pass


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    """Custom social account adapter — populates user fields from Google profile."""

    pass
