from django.urls import path

from . import views

urlpatterns = [
    path("user/cache", views.UserCacheView.as_view(), name="user-cache"),
]
