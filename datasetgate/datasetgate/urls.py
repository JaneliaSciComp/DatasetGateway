"""DatasetGate URL configuration."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("cave_api.urls")),
    path("api/v1/", include("auth_api.urls")),
    path("auth/scim/v2/", include("scim.urls")),
    path("", include("ngauth.urls")),
    path("web/", include("web.urls")),
]
