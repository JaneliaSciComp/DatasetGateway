"""Early settings isolation and strict result accounting for joined tests."""

import json
import os
from pathlib import Path
import tempfile

import pytest

from integration.support import JOINED_MODULE, PROJECT, SELF_CHECK_TEST, redact


def pytest_addoption(parser):
    group = parser.getgroup("DSG joined regression")
    group.addoption("--run-joined", action="store_true", help="Run the explicit DSG/neuPrintHTTP joined module.")
    group.addoption("--neuprint-repo", help="Path to the sibling neuPrintHTTP checkout (unused by ordinary tests).")
    group.addoption("--go", help="Existing Go executable for the joined driver; never install a toolchain.")
    group.addoption("--joined-report", help="Write credential-free joined results and measurements as JSON.")
    group.addoption("--joined-self-check", action="store_true", help="Fault injection: only the invalid-Bearer case with disable-auth.")
    group.addoption("--joined-file-db", action="store_true", help="Use a disposable file-backed SQLite test database.")


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config, parser, args):
    if "--run-joined" not in args:
        return
    folder = tempfile.TemporaryDirectory(prefix="dsg-joined-")
    early_config.add_cleanup(folder.cleanup)
    values = {
        "DSG_JOINED_BOOTSTRAP": "1", "DSG_JOINED_WORKDIR": folder.name,
        "DSG_JOINED_FILE_DB": "1" if "--joined-file-db" in args else "0",
        "DJANGO_SETTINGS_MODULE": "integration.settings",
        "DJANGO_SECRET_KEY": "joined-test-secret-not-for-serving",
        "DJANGO_DEBUG": "True", "DJANGO_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
        "GOOGLE_CLIENT_ID": "joined-test-client", "GOOGLE_CLIENT_SECRET": "joined-test-secret",
        "DATABASE_PATH": str(Path(folder.name) / "unused.sqlite3"),
        "AUTH_COOKIE_DOMAIN": "", "DSG_ORIGIN": "http://127.0.0.1",
    }
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)

    def restore():
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    early_config.add_cleanup(restore)
    early_config._joined_prepared = True


def pytest_configure(config):
    if not config.getoption("run_joined"):
        return
    if not getattr(config, "_joined_prepared", False):
        raise pytest.UsageError("Joined settings must load before Django: add -p integration.pytest_plugin")
    from django.conf import settings
    if settings.SETTINGS_MODULE != "integration.settings":
        raise pytest.UsageError("Joined tests require integration.settings; do not override --ds")
    config.option.liveserver = "127.0.0.1:0"
    config._joined_metrics = []
    config.pluginmanager.register(JoinedResults(config), "dsg-joined-results")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    joined = [item for item in items if Path(item.path).resolve() == JOINED_MODULE]
    if joined and not config.getoption("run_joined"):
        raise pytest.UsageError("Joined module requires -p integration.pytest_plugin --run-joined")


class JoinedResults:
    def __init__(self, config):
        self.config = config
        self.collected = []
        self.reports = []
        self.deselected = []

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items):
        self.collected = [item.nodeid for item in items]
        if any(Path(item.path).resolve() != JOINED_MODULE for item in items):
            raise pytest.UsageError("--run-joined requires only the explicit joined module")
        if self.config.getoption("joined_self_check"):
            if len(items) != 1 or items[0].name != SELF_CHECK_TEST:
                raise pytest.UsageError("--joined-self-check requires only test_invalid_bearer_rejected")
        else:
            if len(self.config.args) != 1 or "::" in self.config.args[0] or Path(self.config.args[0]).resolve() != JOINED_MODULE:
                raise pytest.UsageError("Joined acceptance requires the complete joined module")
        if self.deselected:
            raise pytest.UsageError("Joined acceptance refuses deselected cases")

    def pytest_deselected(self, items):
        self.deselected.extend(item.nodeid for item in items)

    def pytest_runtest_logreport(self, report):
        self.reports.append({"nodeid": report.nodeid, "when": report.when,
                             "outcome": report.outcome, "xfail": hasattr(report, "wasxfail"),
                             "failure": redact(str(report.longrepr)) if report.failed else ""})

    def pytest_sessionfinish(self, session, exitstatus):
        called = {row["nodeid"] for row in self.reports if row["when"] == "call"}
        incomplete = (not self.collected or bool(self.deselected) or called != set(self.collected)
                      or any(row["outcome"] == "skipped" or row["xfail"] for row in self.reports))
        if incomplete:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        report = {"collected": self.collected, "deselected": self.deselected,
                  "incomplete": incomplete, "results": self.reports,
                  "metrics": self.config._joined_metrics, "exitstatus": int(session.exitstatus)}
        destination = self.config.getoption("joined_report")
        if destination:
            path = Path(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2) + "\n")

    def pytest_terminal_summary(self, terminalreporter):
        for metric in self.config._joined_metrics:
            terminalreporter.write_line("JOINED " + json.dumps(metric, sort_keys=True))
