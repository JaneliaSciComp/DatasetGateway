from django.urls import path

from . import views

urlpatterns = [
    path("authorize", views.AuthorizeView.as_view(), name="native-authorize"),
    path("datasets", views.DatasetsView.as_view(), name="native-datasets"),
    path(
        "datasets/<str:name>/versions",
        views.DatasetVersionsView.as_view(),
        name="native-dataset-versions",
    ),
]
