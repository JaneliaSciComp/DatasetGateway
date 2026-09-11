"""First-party explicit HTTP inventory and opt-in passing-test request coverage.

No request bodies, query strings, cookies or credentials are recorded. Coverage
means a passing test made a request, not that every branch or assertion is covered.
"""

from collections import defaultdict
from functools import wraps
import json
from pathlib import Path
from threading import RLock

import pytest

PROJECT = Path(__file__).resolve().parents[2]
SNAPSHOT = Path(__file__).with_name("route_snapshot.json")


def inventory():
    from django.apps import apps
    from django.urls import URLResolver, get_resolver
    from django.views import View

    # Installed first-party apps are direct children of the Django project.
    modules = {app.name for app in apps.get_app_configs()
               if Path(app.path).resolve().parent == PROJECT}
    rows = []

    def visit(patterns, prefix=""):
        for entry in patterns:
            route = prefix + str(entry.pattern)
            if isinstance(entry, URLResolver):
                visit(entry.url_patterns, route)
                continue
            cls = getattr(entry.callback, "view_class", None)
            if cls is None or not any(cls.__module__.startswith(app + ".") for app in modules):
                continue
            for method in View.http_method_names:
                if method in cls.__dict__ and callable(cls.__dict__[method]):
                    rows.append({"route": "/" + route, "method": method.upper(),
                                 "view": cls.__module__ + "." + cls.__name__})

    visit(get_resolver().url_patterns)
    return sorted(rows, key=lambda row: (row["route"], row["method"], row["view"]))


def identity(row):
    return row["route"], row["method"], row["view"]


def pytest_addoption(parser):
    group = parser.getgroup("DSG route coverage")
    group.addoption("--observe-routes", action="store_true",
                    help="Require request coverage of every explicit route in a full ordinary suite.")
    group.addoption("--route-observations", metavar="PATH",
                    help="Write passing-test request associations as JSON (requires --observe-routes).")


def verify_full_invocation(config):
    paths = [Path(arg).resolve() for arg in config.args if "::" not in arg]
    if len(config.args) != 1 or paths != [PROJECT]:
        raise pytest.UsageError("--observe-routes requires the full ordinary suite from the project root")
    filters = ("keyword", "markexpr", "deselect", "ignore", "ignore_glob", "lf",
               "stepwise", "collectonly", "pyargs", "numprocesses")
    if any(config.getoption(name, default=None) for name in filters):
        raise pytest.UsageError("--observe-routes rejects filtered, parallel or collection-only runs")
    if getattr(config, "_override_ini", ()):
        raise pytest.UsageError("--observe-routes rejects configuration overrides")


def pytest_configure(config):
    if config.getoption("route_observations") and not config.getoption("observe_routes"):
        raise pytest.UsageError("--route-observations requires --observe-routes")
    if config.getoption("observe_routes"):
        verify_full_invocation(config)
        observer = RequestObserver(config)
        config.pluginmanager.register(observer, "dsg-request-observer")
        observer.install()
        config.add_cleanup(observer.restore)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    actual = inventory()
    try:
        expected = json.loads(SNAPSHOT.read_text())
    except (OSError, ValueError) as exc:
        raise pytest.UsageError(f"Cannot read route snapshot: {exc}") from exc
    if actual != expected:
        added = sorted(set(map(identity, actual)) - set(map(identity, expected)))
        removed = sorted(set(map(identity, expected)) - set(map(identity, actual)))
        raise pytest.UsageError(
            "Explicit HTTP route snapshot changed; review and update core/tests/route_snapshot.json. "
            f"Added: {added}; removed: {removed}"
        )


class RequestObserver:
    def __init__(self, config):
        self.config = config
        self.lock = RLock()
        self.active_node = None
        self.requests = defaultdict(lambda: defaultdict(set))
        self.passed = set()
        self.disqualified = set()
        self.original = None
        self.wrapper = None
        self.deselected = False
        self.missing = []
        self.total = 0

    def install(self):
        from django.test.client import ClientHandler
        self.original = ClientHandler.__call__

        @wraps(self.original)
        def observe(handler, environ):
            # Capture the active test before the request, including worker threads
            # spawned by that test. Concurrent test execution is refused above.
            with self.lock:
                node = self.active_node
            response = self.original(handler, environ)
            match = response.wsgi_request.resolver_match
            cls = getattr(match.func, "view_class", None) if match else None
            if node is not None and cls is not None:
                key = ("/" + match.route, environ["REQUEST_METHOD"].upper(),
                       cls.__module__ + "." + cls.__name__)
                with self.lock:
                    self.requests[key][node].add(response.status_code)
            return response

        self.wrapper = observe
        ClientHandler.__call__ = observe

    def restore(self):
        from django.test.client import ClientHandler
        if ClientHandler.__call__ is self.wrapper:
            ClientHandler.__call__ = self.original

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        with self.lock:
            self.active_node = item.nodeid
        try:
            return (yield)
        finally:
            with self.lock:
                self.active_node = None

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped or hasattr(report, "wasxfail"):
            self.disqualified.add(report.nodeid)
        elif report.when == "call" and report.passed:
            self.passed.add(report.nodeid)

    def pytest_deselected(self, items):
        self.deselected = True

    def pytest_sessionfinish(self, session, exitstatus):
        self.restore()
        eligible = self.passed - self.disqualified
        rows = []
        for row in inventory():
            associations = [{"nodeid": node, "statuses": sorted(statuses)}
                            for node, statuses in sorted(self.requests[identity(row)].items())
                            if node in eligible]
            rows.append({**row, "tests": associations})
            if not associations:
                self.missing.append(identity(row))
        self.total = len(rows)
        destination = self.config.getoption("route_observations")
        if destination is None:
            destination = PROJECT / ".pytest_cache" / "route-observations.json"
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps({"kind": "passing-test request coverage",
                                           "routes": rows}, indent=2) + "\n")
        if self.missing or self.deselected or not session.testscollected:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter):
        terminalreporter.write_sep("-", f"DSG request coverage: {self.total - len(self.missing)}/{self.total} explicit methods")
        if self.deselected:
            terminalreporter.write_line("ERROR: --observe-routes refuses deselected tests")
        for route, method, view in self.missing:
            terminalreporter.write_line(f"NOT OBSERVED BY A PASSING TEST: {method} {route} ({view})")
