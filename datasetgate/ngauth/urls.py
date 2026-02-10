from django.urls import path

from . import views

urlpatterns = [
    path("", views.IndexView.as_view(), name="ngauth-index"),
    path("health", views.HealthView.as_view(), name="ngauth-health"),
    path("auth/login", views.AuthLoginView.as_view(), name="ngauth-auth-login"),
    path("auth/callback", views.AuthCallbackView.as_view(), name="ngauth-auth-callback"),
    path("login", views.LoginStatusView.as_view(), name="ngauth-login-status"),
    path("logout", views.LogoutView.as_view(), name="ngauth-logout"),
    path("activate", views.ActivateView.as_view(), name="ngauth-activate"),
    path("success", views.SuccessView.as_view(), name="ngauth-success"),
    path("token", views.TokenView.as_view(), name="ngauth-token"),
    path("gcs_token", views.GCSTokenView.as_view(), name="ngauth-gcs-token"),
]
