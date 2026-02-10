from django.urls import path

from . import views

urlpatterns = [
    path("whoami", views.WhoAmIView.as_view(), name="whoami"),
    path("datasets", views.DatasetsListView.as_view(), name="datasets-list"),
    path("datasets/<slug:slug>/versions", views.DatasetVersionsView.as_view(), name="dataset-versions"),
    path("authorize", views.AuthorizeDecisionView.as_view(), name="authorize-decision"),
]
