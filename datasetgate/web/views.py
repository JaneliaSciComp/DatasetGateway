"""Web UI views — dataset browsing, TOS acceptance, grant management."""

import logging

from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from django.conf import settings

logger = logging.getLogger(__name__)

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
    """GET /web/my-datasets — Comprehensive user dashboard."""

    def get(self, request):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        from core.cache import build_permission_cache

        perm_cache = build_permission_cache(user)

        # Groups
        groups = perm_cache["groups"]

        # Datasets this user admins
        admin_datasets = DatasetAdmin.objects.filter(user=user).select_related("dataset")

        # Missing TOS — enrich with invite_token for link generation
        missing_tos = perm_cache["missing_tos"]
        tos_ids = [item["tos_id"] for item in missing_tos]
        tos_tokens = dict(
            TOSDocument.objects.filter(pk__in=tos_ids).values_list("pk", "invite_token")
        )
        for item in missing_tos:
            item["invite_token"] = tos_tokens.get(item["tos_id"], "")

        # Direct grants
        grants = Grant.objects.filter(user=user).select_related(
            "dataset", "permission", "dataset_version"
        ).order_by("-created")

        # Group-based permissions
        group_perms = GroupDatasetPermission.objects.filter(
            group__user_groups__user=user
        ).select_related("group", "dataset", "permission").order_by("dataset__name")

        # TOS acceptances
        acceptances = TOSAcceptance.objects.filter(user=user).select_related(
            "tos_document"
        ).order_by("-accepted_at")

        return render(request, "web/my_datasets.html", {
            "user": user,
            "groups": groups,
            "admin_datasets": admin_datasets,
            "missing_tos": missing_tos,
            "grants": grants,
            "group_perms": group_perms,
            "acceptances": acceptances,
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

            target_user, user_created = User.objects.get_or_create(
                email=email,
                defaults={"name": email.split("@")[0]},
            )
            if user_created:
                target_user.set_unusable_password()
                target_user.save()

            perm = get_object_or_404(Permission, pk=perm_id)
            dv = DatasetVersion.objects.get(pk=version_id) if version_id else None

            _, grant_created = Grant.objects.get_or_create(
                user=target_user,
                dataset=ds,
                dataset_version=dv,
                permission=perm,
                defaults={"granted_by": user, "source": Grant.SOURCE_MANUAL},
            )
            if user_created:
                messages.success(request, f"Created user and granted {perm.name} to {email}")
            elif grant_created:
                messages.success(request, f"Granted {perm.name} to {email}")
            else:
                messages.info(request, f"{email} already has {perm.name}")

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


class TOSLandingView(View):
    """GET/POST /web/tos/<str:invite_token>/ — TOS landing page with access-mode enforcement."""

    def _get_tos_doc(self, invite_token):
        try:
            return TOSDocument.objects.select_related("dataset").get(invite_token=invite_token)
        except TOSDocument.DoesNotExist:
            raise Http404

    def _user_is_authorized(self, user, dataset):
        """Check if user has Grant, DatasetAdmin, or is global admin."""
        if user.admin:
            return True
        if DatasetAdmin.objects.filter(user=user, dataset=dataset).exists():
            return True
        if Grant.objects.filter(user=user, dataset=dataset).exists():
            return True
        return False

    def get(self, request, invite_token):
        user = _get_web_user(request)
        tos_doc = self._get_tos_doc(invite_token)
        dataset = tos_doc.dataset

        already_accepted = False
        if user:
            already_accepted = TOSAcceptance.objects.filter(
                user=user, tos_document=tos_doc
            ).exists()

        if not user:
            return render(request, "web/tos_landing.html", {
                "user": None,
                "tos_doc": tos_doc,
                "dataset": dataset,
                "already_accepted": False,
                "login_next": f"/web/tos/{invite_token}/",
            })

        if dataset and dataset.access_mode == Dataset.ACCESS_CLOSED:
            if not self._user_is_authorized(user, dataset):
                return render(request, "web/tos_landing_denied.html", {
                    "user": user,
                    "dataset": dataset,
                })

        return render(request, "web/tos_landing.html", {
            "user": user,
            "tos_doc": tos_doc,
            "dataset": dataset,
            "already_accepted": already_accepted,
        })

    def post(self, request, invite_token):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        tos_doc = self._get_tos_doc(invite_token)
        dataset = tos_doc.dataset

        # Re-check authorization for closed datasets
        if dataset and dataset.access_mode == Dataset.ACCESS_CLOSED:
            if not self._user_is_authorized(user, dataset):
                return render(request, "web/tos_landing_denied.html", {
                    "user": user,
                    "dataset": dataset,
                })

        # For public datasets, auto-create view grant via self-service
        if dataset and dataset.access_mode == Dataset.ACCESS_PUBLIC:
            view_perm, _ = Permission.objects.get_or_create(name="view")
            Grant.objects.get_or_create(
                user=user,
                dataset=dataset,
                permission=view_perm,
                dataset_version=None,
                defaults={"source": Grant.SOURCE_SELF_SERVICE},
            )

        # Record TOS acceptance
        _, created = TOSAcceptance.objects.get_or_create(
            user=user,
            tos_document=tos_doc,
            defaults={"ip_address": request.META.get("REMOTE_ADDR")},
        )

        if created:
            # Provision bucket IAM for all dataset versions
            if dataset:
                from ngauth.gcs import add_user_to_bucket

                for dv in DatasetVersion.objects.filter(dataset=dataset).exclude(gcs_bucket=""):
                    try:
                        add_user_to_bucket(dv.gcs_bucket, user.email)
                    except Exception:
                        logger.exception(
                            "Failed to provision bucket IAM",
                            extra={"bucket": dv.gcs_bucket, "email": user.email},
                        )

            messages.success(request, f"Accepted: {tos_doc.name}")
        else:
            messages.info(request, f"You have already accepted: {tos_doc.name}")

        return redirect("web-my-datasets")
