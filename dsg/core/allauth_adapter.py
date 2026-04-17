"""Allauth adapters for DatasetGateway.

Bridges allauth's login/signup flow with DatasetGateway's APIKey token system.
"""

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from .models import APIKey


class AccountAdapter(DefaultAccountAdapter):
    """Custom account adapter — creates APIKey on login and handles redirects."""

    def login(self, request, user):
        """Create an APIKey and stash the token value in the session.

        The DSGTokenCookieMiddleware picks it up and sets the dsg_token cookie
        on the response (which we don't control here since allauth redirects).
        """
        super().login(request, user)
        APIKey.objects.filter(user=user, description="allauth login token").delete()
        api_key = APIKey.objects.create(user=user, description="allauth login token")
        request.session["dsg_token_value"] = api_key.key
        request.session["user_email"] = user.email

    def logout(self, request):
        """Flag the request so the middleware clears the dsg_token cookie."""
        request._dsg_logout = True
        super().logout(request)

    def get_login_redirect_url(self, request):
        """Redirect to the URL stored before OAuth, or the default."""
        return request.session.pop("oauth_next", "/web/datasets")

    def get_logout_redirect_url(self, request):
        """After logout, go back to the datasets page."""
        return "/web/datasets"


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    """Custom social account adapter — populates user fields from Google profile."""

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        name = data.get("name") or ""
        if name:
            user.name = name
            user.display_name = name
        return user

    def pre_social_login(self, request, sociallogin):
        super().pre_social_login(request, sociallogin)
        extra = sociallogin.account.extra_data
        picture = extra.get("picture", "")
        if picture and sociallogin.user.pk:
            sociallogin.user.picture_url = picture
            sociallogin.user.save(update_fields=["picture_url"])
