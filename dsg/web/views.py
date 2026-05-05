"""Web UI views — dataset browsing, TOS acceptance, grant management."""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout as auth_logout
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

logger = logging.getLogger(__name__)

from core.audit import log_audit
from core.models import (
    APIKey,
    Dataset,
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


def _has_dataset_admin(user, dataset):
    """Check if user has admin grant on dataset, or is global admin."""
    if user.admin:
        return True
    return Grant.objects.filter(
        user=user, dataset=dataset, permission__name="admin"
    ).exists()


def _can_manage_dataset(user, dataset):
    """Check if user has admin or manage grant on dataset, or is global admin."""
    if user.admin:
        return True
    return Grant.objects.filter(
        user=user, dataset=dataset, permission__name__in=["admin", "manage"]
    ).exists()


def _is_group_admin(user, group):
    """Check if user is admin of a group, or is global admin."""
    if user.admin:
        return True
    return UserGroup.objects.filter(user=user, group=group, is_admin=True).exists()


def _get_web_user(request):
    """Get user from dsg_token cookie (preferred) or session.

    The dsg_token cookie is reset on every successful OAuth callback, so when
    both signals exist and disagree it reflects the most recent identity. We
    repair a stale session entry rather than trust it, which prevents browsers
    that have been logged into multiple Google accounts from acting on the
    wrong user.
    """
    token = request.COOKIES.get(settings.AUTH_COOKIE_NAME)
    if token:
        try:
            api_key = APIKey.objects.select_related("user").get(key=token)
            cookie_user = api_key.user
        except APIKey.DoesNotExist:
            cookie_user = None
        if cookie_user:
            if request.session.get("user_email") != cookie_user.email:
                request.session["user_email"] = cookie_user.email
            return cookie_user

    email = request.session.get("user_email")
    if email:
        try:
            return User.objects.get(email=email)
        except User.DoesNotExist:
            return None

    return None


class LogoutView(View):
    """POST /web/logout — Clear session and dsg_token cookie."""

    def post(self, request):
        auth_logout(request)
        response = redirect("/")
        delete_kwargs = {}
        cookie_domain = getattr(settings, "AUTH_COOKIE_DOMAIN", "")
        if cookie_domain:
            delete_kwargs["domain"] = cookie_domain
        response.delete_cookie(settings.AUTH_COOKIE_NAME, **delete_kwargs)
        return response


class DatasetsView(View):
    """GET /web/datasets — Browse datasets (login required).

    Splits datasets into two sections:
    - "your_datasets": datasets the user has been granted access to (plus
      every closed dataset for global admins, who manage them all).
    - "public_datasets": public datasets the user does not yet have a grant
      on. These are visible to everyone so users can see the dataset and
      accept any required TOS to gain self-service access.
    """

    def get(self, request):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login?next=/web/datasets")

        is_sc_or_admin = user and _is_sc_or_admin(user)

        granted_ids = set(
            Grant.objects.filter(user=user).values_list("dataset_id", flat=True)
        )

        if user.admin:
            your_qs = Dataset.objects.filter(
                Q(pk__in=granted_ids) | Q(access_mode=Dataset.ACCESS_CLOSED)
            ).order_by("name")
        else:
            your_qs = Dataset.objects.filter(pk__in=granted_ids).order_by("name")

        public_qs = (
            Dataset.objects.filter(access_mode=Dataset.ACCESS_PUBLIC)
            .exclude(pk__in=granted_ids)
            .order_by("name")
        )

        # TOS IDs the current user has already accepted
        accepted_tos_ids = set(
            TOSAcceptance.objects.filter(user=user).values_list(
                "tos_document_id", flat=True
            )
        )

        def _build_entry(d):
            versions = DatasetVersion.objects.filter(dataset=d).prefetch_related("buckets")
            tos_docs = list(
                TOSDocument.objects.filter(dataset=d)
                .select_related("service")
                .order_by("service__name", "name")
            )
            for tos in tos_docs:
                tos.accepted = tos.pk in accepted_tos_ids
            return {
                "dataset": d,
                "versions": versions,
                "can_manage": _can_manage_dataset(user, d),
                "has_service_tables": ServiceTable.objects.filter(dataset=d).exists(),
                "tos_docs": tos_docs,
            }

        your_datasets = [_build_entry(d) for d in your_qs]
        public_datasets = [_build_entry(d) for d in public_qs]

        return render(request, "web/datasets.html", {
            "user": user,
            "your_datasets": your_datasets,
            "public_datasets": public_datasets,
            "is_sc_or_admin": is_sc_or_admin,
        })


class DatasetAdminManageView(View):
    """GET/POST /web/dataset-admins/<slug:dataset> — SC promotes dataset admins."""

    def get(self, request, dataset):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        ds = get_object_or_404(Dataset, name=dataset)

        if not _is_sc_or_admin(user):
            return render(request, "web/access_denied.html", {"user": user})

        admin_perm = Permission.objects.filter(name="admin").first()
        admins = Grant.objects.filter(
            dataset=ds, permission=admin_perm
        ).select_related("user").order_by("user__email") if admin_perm else Grant.objects.none()

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

            admin_perm, _ = Permission.objects.get_or_create(name="admin")
            grant, created = Grant.objects.get_or_create(
                user=target_user, dataset=ds, permission=admin_perm,
                defaults={"granted_by": user, "source": Grant.SOURCE_MANUAL},
            )
            if created:
                log_audit(user, "dataset_admin_added", "Grant", grant.pk, after_state={
                    "user": target_user.email, "dataset": ds.name,
                    "permission": "admin", "source": Grant.SOURCE_MANUAL,
                })
                from core.iam import sync_user_dataset_iam
                sync_user_dataset_iam(target_user, ds)
                messages.success(request, f"Added {email} as dataset admin")
            else:
                messages.info(request, f"{email} is already a dataset admin")

        elif action == "remove":
            grant_id = request.POST.get("grant_id")
            admin_perm = Permission.objects.filter(name="admin").first()
            if admin_perm:
                grant = Grant.objects.filter(
                    pk=grant_id, dataset=ds, permission=admin_perm
                ).select_related("user").first()
                if grant:
                    removed_user = grant.user
                    before = {
                        "user": grant.user.email, "dataset": ds.name,
                        "permission": "admin",
                    }
                    grant.delete()
                    log_audit(user, "dataset_admin_removed", "Grant", grant_id, before_state=before)
                    from core.iam import sync_user_dataset_iam
                    sync_user_dataset_iam(removed_user, ds)
            messages.success(request, "Removed dataset admin")

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


class MyAccountView(View):
    """GET /web/my-account — Comprehensive user account dashboard."""

    def get(self, request):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        from core.cache import build_permission_cache

        perm_cache = build_permission_cache(user)

        # Groups — global admins can manage any group
        if user.admin:
            groups = list(Group.objects.values_list("name", flat=True).order_by("name"))
            groups_admin = groups
        else:
            groups = perm_cache["groups"]
            groups_admin = perm_cache["groups_admin"]

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

        # Unified dataset access: merge grants + group_perms into per-dataset rows
        level_map = {"view": 1, "edit": 2, "manage": 3, "admin": 4}
        level_names = {1: "view", 2: "edit", 3: "manage", 4: "admin"}
        ds_info = {}
        for g in grants:
            name = g.dataset.name
            ds_info.setdefault(name, {"level": 0, "sources": set()})
            ds_info[name]["level"] = max(ds_info[name]["level"], level_map.get(g.permission.name, 0))
            ds_info[name]["sources"].add("Direct")
        for gp in group_perms:
            name = gp.dataset.name
            ds_info.setdefault(name, {"level": 0, "sources": set()})
            ds_info[name]["level"] = max(ds_info[name]["level"], level_map.get(gp.permission.name, 0))
            ds_info[name]["sources"].add(f"Group: {gp.group.name}")
        dataset_access = sorted([
            {"dataset_name": n, "role": level_names.get(i["level"], "view"),
             "sources": ", ".join(sorted(i["sources"]))}
            for n, i in ds_info.items()
        ], key=lambda x: x["dataset_name"])

        # TOS acceptances
        acceptances = TOSAcceptance.objects.filter(user=user).select_related(
            "tos_document"
        ).order_by("-accepted_at")

        is_admin = user.admin
        is_sc = UserGroup.objects.filter(user=user, group__name="sc").exists()

        # Long-lived programmatic tokens (expires_at IS NULL).
        # Login tokens (description="OAuth login token" / "allauth login token")
        # have a 7-day expiry and are intentionally excluded.
        api_tokens = APIKey.objects.filter(
            user=user, expires_at__isnull=True
        ).order_by("-created")

        return render(request, "web/my_account.html", {
            "user": user,
            "groups": groups,
            "groups_admin": groups_admin,
            "dataset_access": dataset_access,
            "missing_tos": missing_tos,
            "acceptances": acceptances,
            "is_admin": is_admin,
            "is_sc": is_sc,
            "api_tokens": api_tokens,
        })

    def post(self, request):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        action = request.POST.get("action", "")

        if action == "create_token":
            description = request.POST.get("description", "").strip()
            if not description:
                messages.error(request, "Description is required.")
                return redirect("web-my-account")
            token = APIKey.objects.create(
                user=user, description=description, expires_at=None,
            )
            log_audit(
                user, "api_token_created", "APIKey", token.pk,
                after_state={"user": user.email, "description": description},
            )
            messages.success(request, "Token created.")
            return redirect("web-my-account")

        if action == "revoke_token":
            token_id = request.POST.get("token_id", "")
            try:
                token = APIKey.objects.get(
                    pk=int(token_id), user=user, expires_at__isnull=True,
                )
            except (APIKey.DoesNotExist, ValueError, TypeError):
                messages.error(request, "Token not found.")
                return redirect("web-my-account")
            before = {"user": user.email, "description": token.description}
            token.delete()
            log_audit(
                user, "api_token_revoked", "APIKey", token_id,
                before_state=before,
            )
            messages.success(request, "Token revoked.")
            return redirect("web-my-account")

        messages.error(request, "Unknown action.")
        return redirect("web-my-account")


class GrantManageView(View):
    """GET/POST /web/grants/<slug:dataset> — Dataset members page.

    Requires admin or manage grant on dataset, or global admin.
    Users with manage (but not admin) can grant/revoke up to manage level.
    """

    def _check_access(self, user, ds):
        """Return None if allowed, or a redirect response."""
        if _can_manage_dataset(user, ds):
            return None
        return render(None, "web/access_denied.html", {"user": user})

    def get(self, request, dataset):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        ds = get_object_or_404(Dataset, name=dataset)

        err = self._check_access(user, ds)
        if err:
            return render(request, "web/access_denied.html", {"user": user})

        grants = Grant.objects.filter(dataset=ds).select_related(
            "user", "permission", "dataset_version", "group"
        ).order_by("user__email")

        # Get unique groups for filtering
        group_names = sorted(set(
            g.group.name for g in grants if g.group_id
        ))

        is_admin_user = _has_dataset_admin(user, ds)
        permissions = Permission.objects.all()
        if not is_admin_user:
            permissions = permissions.exclude(name="admin")
        versions = DatasetVersion.objects.filter(dataset=ds)

        return render(request, "web/grant_manage.html", {
            "user": user,
            "dataset": ds,
            "grants": grants,
            "permissions": permissions,
            "versions": versions,
            "group_names": group_names,
            "is_admin_user": is_admin_user,
        })

    def post(self, request, dataset):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        ds = get_object_or_404(Dataset, name=dataset)

        if not _can_manage_dataset(user, ds):
            return render(request, "web/access_denied.html", {"user": user})

        is_admin_user = _has_dataset_admin(user, ds)
        action = request.POST.get("action")

        if action == "grant":
            email = request.POST.get("email", "").strip()
            perm_id = request.POST.get("permission")
            version_id = request.POST.get("version") or None

            perm = get_object_or_404(Permission, pk=perm_id)

            if perm.name == "admin" and not is_admin_user:
                messages.error(request, "You cannot grant admin permission")
                return redirect("web-grant-manage", dataset=dataset)

            target_user, user_created = User.objects.get_or_create(
                email=email,
                defaults={"name": email.split("@")[0]},
            )
            if user_created:
                target_user.set_unusable_password()
                target_user.save()

            dv = DatasetVersion.objects.get(pk=version_id) if version_id else None

            grant, grant_created = Grant.objects.get_or_create(
                user=target_user,
                dataset=ds,
                dataset_version=dv,
                permission=perm,
                defaults={"granted_by": user, "source": Grant.SOURCE_MANUAL},
            )
            if grant_created:
                log_audit(user, "grant_created", "Grant", grant.pk, after_state={
                    "user": target_user.email, "dataset": ds.name,
                    "permission": perm.name,
                    "version": dv.version if dv else None,
                    "source": Grant.SOURCE_MANUAL,
                })
                from core.iam import sync_user_dataset_iam
                sync_user_dataset_iam(target_user, ds)
            if user_created:
                messages.success(request, f"Created user and granted {perm.name} to {email}")
            elif grant_created:
                messages.success(request, f"Granted {perm.name} to {email}")
            else:
                messages.info(request, f"{email} already has {perm.name}")

        elif action == "revoke":
            grant_id = request.POST.get("grant_id")
            qs = Grant.objects.filter(pk=grant_id, dataset=ds)
            if not is_admin_user:
                qs = qs.exclude(permission__name="admin")
            grant = qs.select_related("user", "permission", "dataset_version").first()
            if grant and grant.user == user:
                messages.error(request, "You cannot revoke your own grants")
            elif grant:
                revoked_user = grant.user
                before = {
                    "user": grant.user.email, "dataset": ds.name,
                    "permission": grant.permission.name,
                    "version": grant.dataset_version.version if grant.dataset_version else None,
                    "source": grant.source,
                }
                grant.delete()
                log_audit(user, "grant_revoked", "Grant", grant_id, before_state=before)
                from core.iam import sync_user_dataset_iam
                sync_user_dataset_iam(revoked_user, ds)
                messages.success(request, "Grant revoked")

        return redirect("web-grant-manage", dataset=dataset)


class PublicRootManageView(View):
    """GET/POST /web/public-roots/<slug:dataset> — Service table + public root management."""

    def get(self, request, dataset):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        ds = get_object_or_404(Dataset, name=dataset)

        if not _can_manage_dataset(user, ds):
            return render(request, "web/access_denied.html", {"user": user})

        service_tables = ServiceTable.objects.filter(dataset=ds).prefetch_related("public_roots")
        is_admin_user = _has_dataset_admin(user, ds)

        return render(request, "web/public_roots.html", {
            "user": user,
            "dataset": ds,
            "service_tables": service_tables,
            "is_admin_user": is_admin_user,
        })

    def post(self, request, dataset):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        ds = get_object_or_404(Dataset, name=dataset)

        if not _can_manage_dataset(user, ds):
            return render(request, "web/access_denied.html", {"user": user})

        is_admin_user = _has_dataset_admin(user, ds)
        action = request.POST.get("action")

        if action == "add_service_table":
            if not is_admin_user:
                messages.error(request, "Only dataset administrators can manage service tables")
                return redirect("web-public-roots", dataset=dataset)
            service_name = request.POST.get("service_name", "").strip()
            table_name = request.POST.get("table_name", "").strip()
            if not service_name or not table_name:
                messages.error(request, "Service name and table name are required")
            else:
                st, created = ServiceTable.objects.get_or_create(
                    service_name=service_name, table_name=table_name,
                    defaults={"dataset": ds},
                )
                if created:
                    log_audit(user, "service_table_added", "ServiceTable", st.pk, after_state={
                        "service_name": service_name, "table_name": table_name, "dataset": ds.name,
                    })
                    messages.success(request, f"Added service table {service_name}/{table_name}")
                else:
                    messages.error(request, f"Service table {service_name}/{table_name} already exists")

        elif action == "remove_service_table":
            if not is_admin_user:
                messages.error(request, "Only dataset administrators can manage service tables")
                return redirect("web-public-roots", dataset=dataset)
            st_id = request.POST.get("service_table_id")
            st = ServiceTable.objects.filter(pk=st_id, dataset=ds).first()
            if st:
                before = {
                    "service_name": st.service_name, "table_name": st.table_name,
                    "dataset": ds.name,
                }
                st.delete()
                log_audit(user, "service_table_removed", "ServiceTable", st_id, before_state=before)
                messages.success(request, f"Removed service table {before['service_name']}/{before['table_name']}")

        elif action == "add":
            table_id = request.POST.get("service_table")
            root_id_str = request.POST.get("root_id", "").strip()
            try:
                root_id = int(root_id_str)
                st = ServiceTable.objects.get(pk=table_id, dataset=ds)
                pr, created = PublicRoot.objects.get_or_create(service_table=st, root_id=root_id)
                if created:
                    log_audit(user, "public_root_added", "PublicRoot", pr.pk, after_state={
                        "service_table": f"{st.service_name}/{st.table_name}",
                        "root_id": root_id, "dataset": ds.name,
                    })
                messages.success(request, f"Added public root {root_id}")
            except (ValueError, ServiceTable.DoesNotExist):
                messages.error(request, "Invalid service table or root ID")

        elif action == "remove":
            pr_id = request.POST.get("public_root_id")
            pr = PublicRoot.objects.filter(
                pk=pr_id, service_table__dataset=ds
            ).select_related("service_table").first()
            if pr:
                before = {
                    "service_table": f"{pr.service_table.service_name}/{pr.service_table.table_name}",
                    "root_id": pr.root_id, "dataset": ds.name,
                }
                pr.delete()
                log_audit(user, "public_root_removed", "PublicRoot", pr_id, before_state=before)
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
        """Check if user has Grant or is global admin."""
        if user.admin:
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
            grant, grant_created = Grant.objects.get_or_create(
                user=user,
                dataset=dataset,
                permission=view_perm,
                dataset_version=None,
                defaults={"source": Grant.SOURCE_SELF_SERVICE},
            )
            if grant_created:
                log_audit(user, "grant_created", "Grant", grant.pk, after_state={
                    "user": user.email, "dataset": dataset.name,
                    "permission": "view", "source": Grant.SOURCE_SELF_SERVICE,
                })

        # Record TOS acceptance
        acceptance, created = TOSAcceptance.objects.get_or_create(
            user=user,
            tos_document=tos_doc,
            defaults={"ip_address": request.META.get("REMOTE_ADDR")},
        )

        if created:
            log_audit(user, "tos_accepted", "TOSAcceptance", acceptance.pk, after_state={
                "user": user.email, "tos_document": tos_doc.name,
                "dataset": dataset.name if dataset else None,
            })
            # Provision bucket IAM for all dataset versions
            if dataset:
                from core.iam import sync_user_dataset_iam
                sync_user_dataset_iam(user, dataset)

            messages.success(request, f"Accepted: {tos_doc.name}")
        else:
            messages.info(request, f"You have already accepted: {tos_doc.name}")

        return redirect("web-my-account")


class TOSServiceCheckView(View):
    """GET/POST /web/tos/service-check/ — Accept all pending TOS before redirecting to a service.

    Pending TOS IDs and the redirect URL come from the Django session
    (set by the OAuth callback) or from query params (for already-authenticated
    users redirected by a service).
    """

    def _load_context(self, request):
        """Load pending TOS IDs and redirect URL from session, query, or POST params."""
        tos_ids = request.session.get("tos_check_ids")
        next_url = request.session.get("tos_check_next", "/")

        # POST form may carry the redirect URL from the hidden field
        if request.method == "POST" and request.POST.get("next"):
            next_url = request.POST["next"]

        # Allow query-param override for already-authenticated users
        if not tos_ids:
            service = request.GET.get("service")
            dataset_name = request.GET.get("dataset")
            next_url = request.GET.get("next", next_url)

            if service and dataset_name:
                from django.db.models import Q

                user = _get_web_user(request)
                if user:
                    try:
                        ds = Dataset.objects.get(name=dataset_name)
                    except Dataset.DoesNotExist:
                        return [], next_url

                    tos_user_id = user.parent_id if user.is_service_account else user.pk
                    accepted = set(
                        TOSAcceptance.objects.filter(user_id=tos_user_id).values_list(
                            "tos_document_id", flat=True
                        )
                    )
                    now = timezone.now()
                    pending = []
                    if ds.tos_id and ds.tos_id not in accepted:
                        pending.append(ds.tos_id)
                    svc_ids = list(
                        TOSDocument.objects.filter(
                            service__name=service,
                            dataset=ds,
                            effective_date__lte=now,
                        )
                        .filter(Q(retired_date__isnull=True) | Q(retired_date__gt=now))
                        .exclude(pk__in=accepted)
                        .values_list("pk", flat=True)
                    )
                    pending.extend(svc_ids)
                    tos_ids = pending

        return tos_ids or [], next_url

    def get(self, request):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login?next=/web/tos/service-check/")

        tos_ids, next_url = self._load_context(request)

        if not tos_ids:
            return redirect(next_url)

        tos_docs = TOSDocument.objects.filter(pk__in=tos_ids).select_related("dataset", "service")

        # Check which are already accepted
        accepted_ids = set(
            TOSAcceptance.objects.filter(
                user=user, tos_document_id__in=tos_ids
            ).values_list("tos_document_id", flat=True)
        )
        pending_docs = [d for d in tos_docs if d.pk not in accepted_ids]

        if not pending_docs:
            request.session.pop("tos_check_ids", None)
            request.session.pop("tos_check_next", None)
            return redirect(next_url)

        # Persist to session so the POST handler can find them
        # (query-param mode doesn't set session — only the OAuth callback does)
        request.session["tos_check_ids"] = [d.pk for d in pending_docs]
        request.session["tos_check_next"] = next_url

        return render(request, "web/tos_service_check.html", {
            "user": user,
            "tos_docs": pending_docs,
            "next_url": next_url,
        })

    def post(self, request):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        tos_ids, next_url = self._load_context(request)

        for tos_id in tos_ids:
            try:
                tos_doc = TOSDocument.objects.select_related("dataset").get(pk=tos_id)
            except TOSDocument.DoesNotExist:
                continue

            acceptance, created = TOSAcceptance.objects.get_or_create(
                user=user,
                tos_document=tos_doc,
                defaults={"ip_address": request.META.get("REMOTE_ADDR")},
            )
            if created:
                log_audit(user, "tos_accepted", "TOSAcceptance", acceptance.pk, after_state={
                    "user": user.email,
                    "tos_document": tos_doc.name,
                    "dataset": tos_doc.dataset.name if tos_doc.dataset else None,
                    "service": tos_doc.service.name if tos_doc.service_id else None,
                })
                if tos_doc.dataset:
                    from core.iam import sync_user_dataset_iam
                    sync_user_dataset_iam(user, tos_doc.dataset)

        # Clean up session
        request.session.pop("tos_check_ids", None)
        request.session.pop("tos_check_next", None)

        return redirect(next_url)


class GroupDashboardView(View):
    """GET/POST /web/group/<slug:group_name>/ — Group admin manages group members and grants."""

    def _get_group_and_check_admin(self, request, group_name):
        """Return (user, group) or redirect/access-denied response."""
        user = _get_web_user(request)
        if not user:
            return None, None, redirect("/auth/login")
        group = get_object_or_404(Group, name=group_name)
        if not _is_group_admin(user, group):
            return user, group, render(request, "web/access_denied.html", {"user": user})
        return user, group, None

    def get(self, request, group_name):
        user, group, err = self._get_group_and_check_admin(request, group_name)
        if err:
            return err

        members = UserGroup.objects.filter(group=group).select_related("user").order_by("user__email")

        # Datasets the group admin can manage
        if user.admin:
            managed_datasets = Dataset.objects.all().order_by("name")
        else:
            managed_dataset_ids = Grant.objects.filter(
                user=user, permission__name__in=["admin", "manage"]
            ).values_list("dataset_id", flat=True)
            managed_datasets = Dataset.objects.filter(pk__in=managed_dataset_ids).order_by("name")

        # Grants scoped to this group on managed datasets
        group_grants = Grant.objects.filter(
            group=group, dataset__in=managed_datasets
        ).select_related("user", "dataset", "permission").order_by("user__email", "dataset__name")

        # Available permissions for granting (view, edit, manage — not admin)
        grantable_permissions = Permission.objects.filter(name__in=["view", "edit", "manage"])

        return render(request, "web/group_dashboard.html", {
            "user": user,
            "group": group,
            "members": members,
            "managed_datasets": managed_datasets,
            "group_grants": group_grants,
            "grantable_permissions": grantable_permissions,
        })

    def post(self, request, group_name):
        user, group, err = self._get_group_and_check_admin(request, group_name)
        if err:
            return err

        action = request.POST.get("action")

        if action == "grant":
            email = request.POST.get("email", "").strip()
            dataset_name = request.POST.get("dataset", "").strip()
            perm_name = request.POST.get("permission", "").strip()

            ds = get_object_or_404(Dataset, name=dataset_name)

            # Verify group admin has manage on this dataset
            if not _can_manage_dataset(user, ds):
                messages.error(request, "You do not have manage permission on this dataset")
                return redirect("web-group-dashboard", group_name=group_name)

            # Validate permission level: group admin can grant up to their own level
            perm = get_object_or_404(Permission, name=perm_name)
            level_map = {"view": 1, "edit": 2, "manage": 3, "admin": 4}
            user_level = 0
            for g in Grant.objects.filter(user=user, dataset=ds).select_related("permission"):
                user_level = max(user_level, level_map.get(g.permission.name, 0))
            if user.admin:
                user_level = 4
            if level_map.get(perm_name, 0) > user_level:
                messages.error(request, "Cannot grant a permission level higher than your own")
                return redirect("web-group-dashboard", group_name=group_name)

            # Auto-add to group if not a member
            target_user, user_created = User.objects.get_or_create(
                email=email, defaults={"name": email.split("@")[0]},
            )
            if user_created:
                target_user.set_unusable_password()
                target_user.save()
            UserGroup.objects.get_or_create(user=target_user, group=group)

            grant, grant_created = Grant.objects.get_or_create(
                user=target_user, dataset=ds, permission=perm, group=group,
                defaults={"granted_by": user, "source": Grant.SOURCE_MANUAL},
            )
            if grant_created:
                log_audit(user, "grant_created", "Grant", grant.pk, after_state={
                    "user": target_user.email, "dataset": ds.name,
                    "permission": perm_name, "group": group.name,
                    "source": Grant.SOURCE_MANUAL,
                })
                from core.iam import sync_user_dataset_iam
                sync_user_dataset_iam(target_user, ds)
                messages.success(request, f"Granted {perm_name} on {dataset_name} to {email}")
            else:
                messages.info(request, f"{email} already has {perm_name} on {dataset_name}")

        elif action == "revoke":
            grant_id = request.POST.get("grant_id")
            grant = Grant.objects.filter(
                pk=grant_id, group=group
            ).select_related("user", "dataset", "permission").first()
            if grant:
                revoked_user = grant.user
                revoked_dataset = grant.dataset
                before = {
                    "user": grant.user.email, "dataset": grant.dataset.name,
                    "permission": grant.permission.name, "group": group.name,
                }
                grant.delete()
                log_audit(user, "grant_revoked", "Grant", grant_id, before_state=before)
                from core.iam import sync_user_dataset_iam
                sync_user_dataset_iam(revoked_user, revoked_dataset)
            messages.success(request, "Grant revoked")

        elif action == "add_member":
            email = request.POST.get("email", "").strip()
            target_user, user_created = User.objects.get_or_create(
                email=email, defaults={"name": email.split("@")[0]},
            )
            if user_created:
                target_user.set_unusable_password()
                target_user.save()
            membership, created = UserGroup.objects.get_or_create(user=target_user, group=group)
            if created:
                log_audit(user, "member_added", "UserGroup", membership.pk, after_state={
                    "user": target_user.email, "group": group.name,
                })
                from core.iam import sync_group_datasets_for_user
                sync_group_datasets_for_user(target_user, group)
                messages.success(request, f"Added {email} to {group.name}")
            else:
                messages.info(request, f"{email} is already a member of {group.name}")

        elif action == "remove_member":
            member_id = request.POST.get("member_id")
            try:
                ug = UserGroup.objects.get(pk=member_id, group=group)
                target_user = ug.user
                # Log cascade-deleted grants before removing them
                cascade_grants = Grant.objects.filter(
                    user=target_user, group=group
                ).select_related("dataset", "permission")
                for g in cascade_grants:
                    log_audit(user, "grant_revoked", "Grant", g.pk, before_state={
                        "user": target_user.email, "dataset": g.dataset.name,
                        "permission": g.permission.name, "group": group.name,
                        "reason": "member_removed",
                    })
                cascade_grants.delete()
                log_audit(user, "member_removed", "UserGroup", member_id, before_state={
                    "user": target_user.email, "group": group.name,
                })
                ug.delete()
                from core.iam import sync_group_datasets_for_user
                sync_group_datasets_for_user(target_user, group)
                messages.success(request, f"Removed {target_user.email} from {group.name}")
            except UserGroup.DoesNotExist:
                messages.error(request, "Member not found")

        return redirect("web-group-dashboard", group_name=group_name)
