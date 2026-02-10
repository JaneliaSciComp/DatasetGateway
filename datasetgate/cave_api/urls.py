from django.urls import path

from . import views

urlpatterns = [
    path("user/cache", views.UserCacheView.as_view(), name="user-cache"),
    path(
        "service/<str:namespace>/table/<str:table_id>/dataset",
        views.TableDatasetView.as_view(),
        name="table-dataset",
    ),
    path(
        "user/<int:user_id>/permissions",
        views.UserPermissionsView.as_view(),
        name="user-permissions",
    ),
    path("username", views.UsernameView.as_view(), name="username"),
    path("user", views.UserListView.as_view(), name="user-list"),
    path(
        "table/<str:table_id>/has_public",
        views.TableHasPublicView.as_view(),
        name="table-has-public",
    ),
    path(
        "table/<str:table_id>/root/<int:root_id>/is_public",
        views.RootIsPublicView.as_view(),
        name="root-is-public",
    ),
    path(
        "table/<str:table_id>/root_all_public",
        views.RootAllPublicView.as_view(),
        name="root-all-public",
    ),
]
