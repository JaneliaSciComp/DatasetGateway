from django.urls import path

from . import views

urlpatterns = [
    path("logout", views.LogoutView.as_view(), name="web-logout"),
    path("datasets", views.DatasetsView.as_view(), name="web-datasets"),
    path("tos/<int:tos_id>/accept", views.TOSAcceptView.as_view(), name="web-tos-accept"),
    path("tos/<str:invite_token>/", views.TOSLandingView.as_view(), name="web-tos-landing"),
    path("my-datasets", views.MyDatasetsView.as_view(), name="web-my-datasets"),
    path("grants/<slug:dataset>", views.GrantManageView.as_view(), name="web-grant-manage"),
    path("team-leads/<slug:dataset>", views.TeamLeadManageView.as_view(), name="web-team-lead-manage"),
    path("public-roots/<slug:dataset>", views.PublicRootManageView.as_view(), name="web-public-roots"),
    path("team/<slug:group_name>/", views.TeamDashboardView.as_view(), name="web-team-dashboard"),
]
