from django.urls import path

from . import views

urlpatterns = [
    path("datasets", views.DatasetsView.as_view(), name="web-datasets"),
    path("tos/<int:tos_id>/accept", views.TOSAcceptView.as_view(), name="web-tos-accept"),
    path("my-datasets", views.MyDatasetsView.as_view(), name="web-my-datasets"),
    path("grants/<slug:dataset>", views.GrantManageView.as_view(), name="web-grant-manage"),
    path("public-roots/<slug:dataset>", views.PublicRootManageView.as_view(), name="web-public-roots"),
]
