"""Web UI views — dataset browsing, TOS acceptance, grant management."""

import json

from django.contrib import messages
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from django.conf import settings

from core.models import (
    APIKey,
    Dataset,
    DatasetAdmin,
    DatasetVersion,
    Grant,
    Group,
    GroupDatasetPermission,
    Permission,
    PublicRoot,
    ServiceTable,
    TOSAcceptance,
    TOSDocument,
    User,
    UserGroup,
)


def _is_sc_or_admin(user):
    """Check if user is a global admin or member of the 'sc' group."""
    if user.admin:
        return True
    return UserGroup.objects.filter(user=user, group__name="sc").exists()


def _get_web_user(request):
    """Get user from session or dsg_token cookie."""
    # 1. Session email
    email = request.session.get("user_email")
    if email:
        try:
            return User.objects.get(email=email)
        except User.DoesNotExist:
            return None

    # 2. dsg_token cookie (APIKey lookup)
    token = request.COOKIES.get(settings.AUTH_COOKIE_NAME)
    if token:
        try:
            api_key = APIKey.objects.select_related("user").get(key=token)
            return api_key.user
        except APIKey.DoesNotExist:
            pass

    return None


class DatasetsView(View):
    """GET /web/datasets — Browse datasets."""

    def get(self, request):
        user = _get_web_user(request)
        datasets = Dataset.objects.all().order_by("name")

        is_sc_or_admin = user and _is_sc_or_admin(user)

        dataset_list = []
        for d in datasets:
            versions = DatasetVersion.objects.filter(dataset=d)
            is_admin = False
            if user:
                is_admin = user.admin or DatasetAdmin.objects.filter(
                    user=user, dataset=d
                ).exists()
            dataset_list.append({
                "dataset": d,
                "versions": versions,
                "is_admin": is_admin,
            })

        return render(request, "web/datasets.html", {
            "user": user,
            "dataset_list": dataset_list,
            "is_sc_or_admin": is_sc_or_admin,
        })


class DatasetAdminManageView(View):
    """GET/POST /web/dataset-admins/<slug:dataset> — SC promotes lab heads."""

    def get(self, request, dataset):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        ds = get_object_or_404(Dataset, name=dataset)

        if not _is_sc_or_admin(user):
            return render(request, "web/access_denied.html", {"user": user})

        admins = DatasetAdmin.objects.filter(dataset=ds).select_related("user").order_by("user__email")

        return render(request, "web/dataset_admin_manage.html", {
            "user": user,
            "dataset": ds,
            "admins": admins,
        })

    def post(self, request, dataset):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        ds = get_object_or_404(Dataset, name=dataset)

        if not _is_sc_or_admin(user):
            return render(request, "web/access_denied.html", {"user": user})

        action = request.POST.get("action")

        if action == "add":
            email = request.POST.get("email", "").strip()
            try:
                target_user = User.objects.get(email=email)
            except User.DoesNotExist:
                messages.error(request, f"User not found: {email}")
                return redirect("web-dataset-admin-manage", dataset=dataset)

            _, created = DatasetAdmin.objects.get_or_create(user=target_user, dataset=ds)
            if created:
                messages.success(request, f"Added {email} as lab head")
            else:
                messages.info(request, f"{email} is already a lab head")

        elif action == "remove":
            admin_id = request.POST.get("admin_id")
            DatasetAdmin.objects.filter(pk=admin_id, dataset=ds).delete()
            messages.success(request, "Removed lab head")

        return redirect("web-dataset-admin-manage", dataset=dataset)


class TOSAcceptView(View):
    """GET/POST /web/tos/<int:tos_id>/accept — TOS acceptance flow."""

    def get(self, request, tos_id):
        user = _get_web_user(request)
        tos_doc = get_object_or_404(TOSDocument, pk=tos_id)
        already_accepted = False
        if user:
            already_accepted = TOSAcceptance.objects.filter(
                user=user, tos_document=tos_doc
            ).exists()

        return render(request, "web/tos_accept.html", {
            "user": user,
            "tos_doc": tos_doc,
            "already_accepted": already_accepted,
        })

    def post(self, request, tos_id):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        tos_doc = get_object_or_404(TOSDocument, pk=tos_id)
        TOSAcceptance.objects.get_or_create(
            user=user,
            tos_document=tos_doc,
            defaults={"ip_address": request.META.get("REMOTE_ADDR")},
        )
        messages.success(request, f"Accepted: {tos_doc.name}")
        return redirect("web-datasets")


class MyDatasetsView(View):
    """GET /web/my-datasets — User's accepted datasets and history."""

    def get(self, request):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        acceptances = TOSAcceptance.objects.filter(user=user).select_related(
            "tos_document"
        ).order_by("-accepted_at")

        grants = Grant.objects.filter(user=user).select_related(
            "dataset", "permission", "dataset_version"
        ).order_by("-created")

        return render(request, "web/my_datasets.html", {
            "user": user,
            "acceptances": acceptances,
            "grants": grants,
        })


class GrantManageView(View):
    """GET/POST /web/grants/<slug:dataset> — Team lead grant management."""

    def get(self, request, dataset):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        ds = get_object_or_404(Dataset, name=dataset)

        # Check if user is admin of this dataset or global admin
        if not (user.admin or DatasetAdmin.objects.filter(user=user, dataset=ds).exists()):
            return render(request, "web/access_denied.html", {"user": user})

        grants = Grant.objects.filter(dataset=ds).select_related(
            "user", "permission", "dataset_version"
        ).order_by("user__email")

        permissions = Permission.objects.all()
        versions = DatasetVersion.objects.filter(dataset=ds)

        return render(request, "web/grant_manage.html", {
            "user": user,
            "dataset": ds,
            "grants": grants,
            "permissions": permissions,
            "versions": versions,
        })

    def post(self, request, dataset):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        ds = get_object_or_404(Dataset, name=dataset)

        if not (user.admin or DatasetAdmin.objects.filter(user=user, dataset=ds).exists()):
            return render(request, "web/access_denied.html", {"user": user})

        action = request.POST.get("action")

        if action == "grant":
            email = request.POST.get("email", "").strip()
            perm_id = request.POST.get("permission")
            version_id = request.POST.get("version") or None

            try:
                target_user = User.objects.get(email=email)
            except User.DoesNotExist:
                messages.error(request, f"User not found: {email}")
                return redirect("web-grant-manage", dataset=dataset)

            perm = get_object_or_404(Permission, pk=perm_id)
            dv = DatasetVersion.objects.get(pk=version_id) if version_id else None

            Grant.objects.get_or_create(
                user=target_user,
                dataset=ds,
                dataset_version=dv,
                permission=perm,
                defaults={"granted_by": user},
            )
            messages.success(request, f"Granted {perm.name} to {email}")

        elif action == "revoke":
            grant_id = request.POST.get("grant_id")
            Grant.objects.filter(pk=grant_id, dataset=ds).delete()
            messages.success(request, "Grant revoked")

        return redirect("web-grant-manage", dataset=dataset)


class PublicRootManageView(View):
    """GET/POST /web/public-roots/<slug:dataset> — Public root management."""

    def get(self, request, dataset):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        ds = get_object_or_404(Dataset, name=dataset)

        if not (user.admin or DatasetAdmin.objects.filter(user=user, dataset=ds).exists()):
            return render(request, "web/access_denied.html", {"user": user})

        service_tables = ServiceTable.objects.filter(dataset=ds).prefetch_related("public_roots")

        return render(request, "web/public_roots.html", {
            "user": user,
            "dataset": ds,
            "service_tables": service_tables,
        })

    def post(self, request, dataset):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        ds = get_object_or_404(Dataset, name=dataset)

        if not (user.admin or DatasetAdmin.objects.filter(user=user, dataset=ds).exists()):
            return render(request, "web/access_denied.html", {"user": user})

        action = request.POST.get("action")

        if action == "add":
            table_id = request.POST.get("service_table")
            root_id_str = request.POST.get("root_id", "").strip()
            try:
                root_id = int(root_id_str)
                st = ServiceTable.objects.get(pk=table_id, dataset=ds)
                PublicRoot.objects.get_or_create(service_table=st, root_id=root_id)
                messages.success(request, f"Added public root {root_id}")
            except (ValueError, ServiceTable.DoesNotExist):
                messages.error(request, "Invalid service table or root ID")

        elif action == "remove":
            pr_id = request.POST.get("public_root_id")
            PublicRoot.objects.filter(
                pk=pr_id, service_table__dataset=ds
            ).delete()
            messages.success(request, "Removed public root")

        return redirect("web-public-roots", dataset=dataset)
