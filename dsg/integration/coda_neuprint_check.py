"""Real consent → native DSG auth → neuPrint custom query, explicitly opt-in.

Invoke with -p integration.pytest_plugin --run-joined and --neuprint-repo.
This filename is deliberately outside ordinary pytest discovery.
"""

from datetime import timedelta
import json
import re
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from core.models import (APIKey, AuditLog, ClientConsent, Dataset, DatasetTranslation,
                         DatasetVersion, Grant, Permission, RegisteredClient, Service,
                         TOSAcceptance, TOSDocument, User)
from integration.support import Driver, HarnessError, build_driver, resolve_go, resolve_repo

pytestmark = pytest.mark.django_db(transaction=True)
ORIGIN = "http://127.0.0.1:8765"
QUERY = "RETURN 'predeploy-probe'"


def payload(response):
    match = re.search(r'<script id="ngauth-payload" type="application/json">(.*?)</script>',
                      response.content.decode(), re.DOTALL)
    return json.loads(match[1]) if match else None


class World:
    def __init__(self):
        cache.clear()
        self.user = User.objects.create(email="joined-owner@example.org", name="Joined owner")
        self.admin = User.objects.create(email="joined-admin@example.org", name="Joined admin", admin=True)
        self.registered = RegisteredClient.objects.create(name="Fixture CODA", origin=ORIGIN, owner="Regression suite")
        self.service = Service.objects.create(name="neuprint", version_eval_mode="linear")
        self.datasets = {}
        self.names = {}
        view, _ = Permission.objects.get_or_create(name="view")
        for label in ("private", "denied", "tos", "public"):
            dataset = Dataset.objects.create(name="joined-" + label,
                                             access_mode="public" if label == "public" else "closed")
            version = DatasetVersion.objects.create(dataset=dataset, version="v1", ordinal=1, is_public=label == "public")
            DatasetTranslation.objects.create(service=self.service, client_name=dataset.name, dataset=dataset)
            DatasetTranslation.objects.create(service=self.service, client_name=dataset.name, client_version="v1",
                                               dataset=dataset, dataset_version=version)
            self.datasets[label] = dataset
            self.names[label] = dataset.name + ":v1"
            if label in {"private", "tos"}:
                Grant.objects.create(user=self.user, dataset=dataset, dataset_version=version,
                                     service=self.service, permission=view)
        self.terms = TOSDocument.objects.create(name="Joined terms", text="Synthetic terms.",
                                                dataset=self.datasets["tos"], service=self.service)
        self.browsers = {}
        self.normal_keys = {}
        self.driver = None

    def browser(self, user=None):
        user = user or self.user
        if user.pk not in self.browsers:
            browser = Client(enforce_csrf_checks=True)
            normal = APIKey.objects.create(user=user)
            browser.cookies[settings.AUTH_COOKIE_NAME] = normal.key
            self.browsers[user.pk] = browser
            self.normal_keys[user.pk] = normal
        return self.browsers[user.pk]

    def post(self, browser, path, data):
        return browser.post(path, {**data, "csrfmiddlewaretoken": browser.cookies[settings.CSRF_COOKIE_NAME].value})

    def issue(self, user=None):
        user = user or self.user
        browser = self.browser(user)
        before = APIKey.objects.count()
        response = browser.get("/login", {"origin": ORIGIN, "token": "api"})
        assert response.status_code == 200
        assert payload(response) is None
        assert APIKey.objects.count() == before
        response = self.post(browser, "/login", {"origin": ORIGIN, "token": "api", "decision": "allow"})
        assert response.status_code == 200
        delivered = payload(response)
        if not isinstance(delivered, dict) or set(delivered) != {"token"} or not isinstance(delivered["token"], str):
            pytest.fail("Consent POST did not deliver a credential", pytrace=False)
        key = APIKey.objects.get(key=delivered["token"])
        assert key.user_id == user.pk
        assert key.delegated_client_id == self.registered.pk
        assert not key.is_expired
        assert APIKey.objects.count() == before + 1
        return key

    def revoke(self, key, user=None):
        key_id = key.pk
        response = self.post(self.browser(user), "/web/my-account", {"action": "revoke_delegated", "token_id": key_id})
        assert response.status_code == 302
        assert response["Location"] == "/web/my-account"
        assert not APIKey.objects.filter(pk=key_id).exists()
        assert AuditLog.objects.filter(action="api_token_revoked", target_id=str(key_id)).exists()

    def query(self, label, key=None, *, timeout=3):
        return self.driver.request("/api/custom/custom", bearer=key, timeout=timeout,
                                   data={"dataset": self.names[label], "cypher": QUERY, "version": "0.5.0"})

    def backend(self):
        # Production metadata warmers call GetMain during startup. Every call is
        # retained in the control surface; custom's GetDataset calls are the
        # backend executions attributable to the tested HTTP route.
        return [row for row in self.driver.state()["queries"] if row["accessor"] == "GetDataset"]

    def successful_query(self, label, key, email):
        before = len(self.backend())
        status, body, headers = self.query(label, key)
        assert status == 200
        assert body["columns"] == ["fixture", "dataset"]
        assert body["data"] == [["predeploy-ok", self.names[label]]]
        assert headers.get("X-Predeploy-Identity", "") == email
        rows = self.backend()
        assert len(rows) == before + 1
        assert rows[-1] == {"dataset": self.names[label], "query": QUERY, "read_only": True, "accessor": "GetDataset"}


@pytest.fixture
def world(transactional_db):
    return World()


@pytest.fixture(scope="session")
def driver_binary(request, tmp_path_factory):
    repo = resolve_repo(request.config.getoption("neuprint_repo"))
    go, version = resolve_go(repo, request.config.getoption("go"))
    print(f"Joined Go: {go} ({version})")
    binary = build_driver(repo, go, tmp_path_factory.mktemp("neuprint-build") / "neuprint-predeploy.test")
    return repo, binary


@pytest.fixture
def joined(world, live_server, driver_binary, tmp_path, request):
    repo, binary = driver_binary
    # Fail on any accidental cloud credential discovery. TOS IAM is mocked
    # explicitly at its call site; native DSG HTTP calls remain real.
    with patch("google.auth.default", side_effect=AssertionError("Unexpected cloud operation")):
        world.driver = Driver(binary, repo, tmp_path, live_server.url, list(world.names.values()),
                              disable_auth=request.config.getoption("joined_self_check"))
        try:
            yield world
        finally:
            world.driver.close()


@pytest.fixture
def measure(request):
    def record(world, scenario, **extra):
        state = world.driver.state()
        request.config._joined_metrics.append({
            "case": scenario, "ttl_seconds": state["ttl_seconds"],
            "custom_backend_calls": len([row for row in state["queries"] if row["accessor"] == "GetDataset"]),
            "metadata_backend_calls": len([row for row in state["queries"] if row["accessor"] == "GetMain"]),
            **extra,
        })
    return record


def test_consent_query_and_identity(joined, measure):
    key = joined.issue()
    status, profile, _ = joined.driver.request("/profile", bearer=key.key)
    assert status == 200
    assert profile["Email"] == joined.user.email
    assert profile["AuthLevel"] == "readwrite"
    joined.successful_query("private", key.key, joined.user.email)
    assert len(joined.backend()) == 1
    measure(joined, "consent_query_identity")


def test_invalid_bearer_rejected(joined, measure):
    status, _, _ = joined.query("public", "malformed-credential")
    calls = len(joined.backend())
    if status == 200 and calls > 0:
        measure(joined, "invalid_bearer", fault="SELF_CHECK_BACKEND_REACHED", status=status)
        pytest.fail("SELF_CHECK_BACKEND_REACHED: invalid Bearer query succeeded and reached the backend", pytrace=False)
    assert status == 401
    assert calls == 0
    measure(joined, "invalid_bearer", status=status)


@pytest.mark.parametrize("kind", ["expired", "disabled"])
def test_unusable_credentials_rejected_before_first_use(joined, measure, kind):
    if kind == "disabled":
        user = User.objects.create(email="joined-disabled@example.org")
        key = joined.issue(user)
        user.is_active = False
        user.save(update_fields=["is_active"])
    else:
        key = joined.issue()
        key.expires_at = timezone.now() - timedelta(seconds=1)
        key.save(update_fields=["expires_at"])
    status, _, _ = joined.query("public", key.key)
    assert status == 401
    assert joined.backend() == []
    measure(joined, kind, status=status)


def test_authenticated_dataset_denial(joined, measure):
    key = joined.issue()
    status, _, headers = joined.query("denied", key.key)
    assert status == 403
    assert headers["X-Predeploy-Identity"] == joined.user.email
    assert joined.backend() == []
    measure(joined, "dataset_denial", status=status)


def test_tos_acceptance_recovers_query(joined, measure):
    key = joined.issue()
    status, body, _ = joined.query("tos", key.key)
    assert status == 403
    assert body["tos_required"] is True
    assert joined.backend() == []
    browser = joined.browser()
    page = browser.get(f"/web/tos/{joined.terms.pk}/accept")
    assert page.status_code == 200
    assert not page.context["already_accepted"]
    with patch("core.iam.sync_user_dataset_iam") as iam:
        response = joined.post(browser, f"/web/tos/{joined.terms.pk}/accept", {})
    assert response.status_code == 302
    iam.assert_called_once_with(joined.user, joined.datasets["tos"])
    assert TOSAcceptance.objects.filter(user=joined.user, tos_document=joined.terms).count() == 1
    assert AuditLog.objects.filter(action="tos_accepted").count() == 1
    joined.successful_query("tos", key.key, joined.user.email)
    measure(joined, "tos_recovery", statuses=[403, 200])


def test_delegated_admin_cannot_bypass_dataset_grants(joined, measure):
    delegated = joined.issue(joined.admin)
    status, _, headers = joined.query("denied", delegated.key)
    assert status == 403
    assert headers["X-Predeploy-Identity"] == joined.admin.email
    assert joined.backend() == []
    joined.successful_query("denied", joined.normal_keys[joined.admin.pk].key, joined.admin.email)
    joined.admin.refresh_from_db()
    assert joined.admin.admin is True
    measure(joined, "delegated_admin", statuses=[403, 200])


def test_public_access_refuses_bad_and_revoked_bearers(joined, measure):
    joined.successful_query("public", None, "")
    status, _, _ = joined.query("public", "malformed-credential")
    assert status == 401
    assert len(joined.backend()) == 1
    key = joined.issue()
    joined.revoke(key)
    status, _, _ = joined.query("public", key.key)
    assert status == 401
    assert len(joined.backend()) == 1
    measure(joined, "public_without_anonymous_fallback", statuses=[200, 401, 401])


@pytest.mark.parametrize("decision", ["cancel", "unknown_origin"])
def test_consent_rejection_yields_no_key(joined, measure, decision):
    browser = joined.browser()
    page = browser.get("/login", {"origin": ORIGIN, "token": "api"})
    assert page.status_code == 200
    before = APIKey.objects.count()
    origin = "http://127.0.0.1:9876" if decision == "unknown_origin" else ORIGIN
    response = joined.post(browser, "/login", {"origin": origin, "token": "api",
                                               "decision": "allow" if decision == "unknown_origin" else "cancel"})
    assert response.status_code == 200
    assert payload(response) == ("badorigin" if decision == "unknown_origin" else None)
    assert APIKey.objects.count() == before
    assert not APIKey.objects.filter(delegated_client=joined.registered).exists()
    assert not ClientConsent.objects.exists()
    assert joined.backend() == []
    measure(joined, decision)
