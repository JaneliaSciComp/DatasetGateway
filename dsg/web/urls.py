from django.urls import path

from . import views

urlpatterns = [
    path("logout", views.LogoutView.as_view(), name="web-logout"),
    path("datasets", views.DatasetsView.as_view(), name="web-datasets"),
    path("tos/<int:tos_id>/accept", views.TOSAcceptView.as_view(), name="web-tos-accept"),
    path("tos/service-check/", views.TOSServiceCheckView.as_view(), name="web-tos-service-check"),
    path("tos/<str:invite_token>/", views.TOSLandingView.as_view(), name="web-tos-landing"),
    path("my-account", views.MyAccountView.as_view(), name="web-my-account"),
    path("grants/<slug:dataset>", views.GrantManageView.as_view(), name="web-grant-manage"),
    path("dataset-admins/<slug:dataset>", views.DatasetAdminManageView.as_view(), name="web-dataset-admin-manage"),
    path("public-roots/<slug:dataset>", views.PublicRootManageView.as_view(), name="web-public-roots"),
    path("group/<slug:group_name>/", views.GroupDashboardView.as_view(), name="web-group-dashboard"),
]
