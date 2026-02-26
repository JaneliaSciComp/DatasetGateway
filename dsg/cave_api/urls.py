from django.urls import path

from . import oauth_views, views

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
        "groups/<str:group_name>/members",
        views.GroupMembersView.as_view(),
        name="group-members",
    ),
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
    # OAuth flow
    path("authorize", oauth_views.AuthorizeView.as_view(), name="authorize"),
    path("oauth2callback", oauth_views.OAuth2CallbackView.as_view(), name="oauth2callback"),
    path("logout", oauth_views.LogoutView.as_view(), name="logout"),
    # Token management
    path("create_token", oauth_views.CreateTokenView.as_view(), name="create-token"),
    path("user/token", oauth_views.UserTokensView.as_view(), name="user-tokens"),
    path("refresh_token", oauth_views.RefreshTokenView.as_view(), name="refresh-token"),
]
