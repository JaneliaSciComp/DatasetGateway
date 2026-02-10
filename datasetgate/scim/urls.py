from django.urls import path

from . import views

urlpatterns = [
    # Discovery
    path("ServiceProviderConfig", views.ServiceProviderConfigView.as_view(), name="scim-spc"),
    path("ResourceTypes", views.ResourceTypesView.as_view(), name="scim-resource-types"),
    path("Schemas", views.SchemasView.as_view(), name="scim-schemas"),
    # Users
    path("Users", views.UserListView.as_view(), name="scim-users"),
    path("Users/<str:scim_id>", views.UserDetailView.as_view(), name="scim-user-detail"),
    # Groups
    path("Groups", views.GroupListView.as_view(), name="scim-groups"),
    path("Groups/<str:scim_id>", views.GroupDetailView.as_view(), name="scim-group-detail"),
    # Datasets
    path("Datasets", views.DatasetListView.as_view(), name="scim-datasets"),
    path("Datasets/<str:scim_id>", views.DatasetDetailView.as_view(), name="scim-dataset-detail"),
]
