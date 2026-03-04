"""Web UI views — dataset browsing, TOS acceptance, grant management."""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout as auth_logout
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
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
    """GET /web/datasets — Browse datasets (login required)."""

    def get(self, request):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login?next=/web/datasets")

        is_sc_or_admin = user and _is_sc_or_admin(user)

        if user.admin:
            datasets = Dataset.objects.all().order_by("name")
        else:
            granted_ids = Grant.objects.filter(user=user).values_list(
                "dataset_id", flat=True
            )
            datasets = Dataset.objects.filter(pk__in=granted_ids).order_by("name")

        dataset_list = []
        for d in datasets:
            versions = DatasetVersion.objects.filter(dataset=d)
            can_manage = _can_manage_dataset(user, d)
            dataset_list.append({
                "dataset": d,
                "versions": versions,
                "can_manage": can_manage,
            })

        return render(request, "web/datasets.html", {
            "user": user,
            "dataset_list": dataset_list,
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
                    before = {
                        "user": grant.user.email, "dataset": ds.name,
                        "permission": "admin",
                    }
                    grant.delete()
                    log_audit(user, "dataset_admin_removed", "Grant", grant_id, before_state=before)
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

        # Groups
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

        return render(request, "web/my_account.html", {
            "user": user,
            "groups": groups,
            "groups_admin": groups_admin,
            "dataset_access": dataset_access,
            "missing_tos": missing_tos,
            "acceptances": acceptances,
            "is_admin": is_admin,
            "is_sc": is_sc,
        })


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
                before = {
                    "user": grant.user.email, "dataset": ds.name,
                    "permission": grant.permission.name,
                    "version": grant.dataset_version.version if grant.dataset_version else None,
                    "source": grant.source,
                }
                grant.delete()
                log_audit(user, "grant_revoked", "Grant", grant_id, before_state=before)
                messages.success(request, "Grant revoked")

        return redirect("web-grant-manage", dataset=dataset)


class PublicRootManageView(View):
    """GET/POST /web/public-roots/<slug:dataset> — Public root management."""

    def get(self, request, dataset):
        user = _get_web_user(request)
        if not user:
            return redirect("/auth/login")

        ds = get_object_or_404(Dataset, name=dataset)

        if not _can_manage_dataset(user, ds):
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

        if not _can_manage_dataset(user, ds):
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

        return redirect("web-my-account")


class GroupDashboardView(View):
    """GET/POST /web/group/<slug:group_name>/ — Group admin manages group members and grants."""

    def _get_group_and_check_admin(self, request, group_name):
        """Return (user, group) or redirect/access-denied response."""
        user = _get_web_user(request)
        if not user:
            return None, None, redirect("/auth/login")
        group = get_object_or_404(Group, name=group_name)
        if not (user.admin or UserGroup.objects.filter(user=user, group=group, is_admin=True).exists()):
            return user, group, render(request, "web/access_denied.html", {"user": user})
        return user, group, None

    def get(self, request, group_name):
        user, group, err = self._get_group_and_check_admin(request, group_name)
        if err:
            return err

        members = UserGroup.objects.filter(group=group).select_related("user").order_by("user__email")

        # Datasets the group admin can manage (has manage or admin grant)
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
                messages.success(request, f"Granted {perm_name} on {dataset_name} to {email}")
            else:
                messages.info(request, f"{email} already has {perm_name} on {dataset_name}")

        elif action == "revoke":
            grant_id = request.POST.get("grant_id")
            grant = Grant.objects.filter(
                pk=grant_id, group=group
            ).select_related("user", "dataset", "permission").first()
            if grant:
                before = {
                    "user": grant.user.email, "dataset": grant.dataset.name,
                    "permission": grant.permission.name, "group": group.name,
                }
                grant.delete()
                log_audit(user, "grant_revoked", "Grant", grant_id, before_state=before)
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
                messages.success(request, f"Removed {target_user.email} from {group.name}")
            except UserGroup.DoesNotExist:
                messages.error(request, "Member not found")

        return redirect("web-group-dashboard", group_name=group_name)
