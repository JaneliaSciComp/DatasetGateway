"""Gate failure controls that require neither Go, sockets nor a sibling repo."""

import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from integration import support
from integration.pytest_plugin import JoinedResults

spec = importlib.util.spec_from_file_location("predeploy_runner", support.PROJECT / "scripts" / "test-predeploy.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "neuprint"
    root.mkdir()
    for name, content in {"go.mod": "module fixture\n\ntoolchain go1.24.1\n", "go.sum": "", "main.go": "package main\n", "predeploy_driver_test.go": "package main\n"}.items():
        (root / name).write_text(content)
    return root


@pytest.mark.parametrize("missing, message", [
    ("checkout", "checkout or predeploy driver missing"),
    ("gpg", "Required executable missing: gpg"),
    ("gpgconf", "Required matching executable missing: gpgconf"),
    ("go", "Go toolchain unavailable"),
])
def test_runner_missing_prerequisite_exits_with_reason(checkout, tmp_path, missing, message):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("gpg", "gpgconf"):
        if missing == name:
            continue
        path = bin_dir / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o700)
    repo = checkout / "absent" if missing == "checkout" else checkout
    code, output = support.run_command([
        sys.executable, str(support.PROJECT / "scripts" / "test-predeploy.py"),
        "--neuprint-repo", str(repo), "--go", str(tmp_path / "absent-go"),
    ], cwd=support.PROJECT, timeout=15, env={**os.environ, "PATH": str(bin_dir)})
    assert code == 2
    assert message in output


@pytest.mark.parametrize("system, machine, expected", [
    ("Darwin", "arm64", ("darwin", "arm64")),
    ("Linux", "aarch64", ("linux", "arm64")),
    ("Linux", "x86_64", ("linux", "amd64")),
])
def test_go_cache_platform_comes_from_host(monkeypatch, system, machine, expected):
    monkeypatch.setattr(support.platform, "system", lambda: system)
    monkeypatch.setattr(support.platform, "machine", lambda: machine)
    assert support.host_go_platform() == expected


def test_cached_toolchain_resolution_is_offline(monkeypatch, checkout, tmp_path):
    cache = tmp_path / "modules"
    candidate = cache / "golang.org/toolchain@v0.0.1-go1.24.1.linux-arm64/bin/go"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("test fixture, not a real Go toolchain")
    candidate.chmod(0o700)
    monkeypatch.delenv("GO", raising=False)
    monkeypatch.setenv("GOMODCACHE", str(cache))
    monkeypatch.setattr(support.shutil, "which", lambda name: None)
    monkeypatch.setattr(support, "host_go_platform", lambda: ("linux", "arm64"))
    def version(args, **kwargs):
        assert args == [str(candidate), "version"]
        assert {key: kwargs["env"][key] for key in ("GOTOOLCHAIN", "GOPROXY", "GOSUMDB")} == {
            "GOTOOLCHAIN": "local", "GOPROXY": "off", "GOSUMDB": "off",
        }
        return 0, "go version go1.24.1 linux/arm64\n"
    monkeypatch.setattr(support, "run_command", version)
    assert support.resolve_go(checkout)[0] == candidate


def test_protected_dependency_changes_fail(checkout):
    guard = support.LockGuard(checkout)
    (checkout / "go.sum").write_text("unexpected change")
    with pytest.raises(support.HarnessError, match="Protected dependency file changed"):
        guard.check()


def report_for(outcome="passed", *, xfail=False):
    node = "integration/coda_neuprint_check.py::test_invalid_bearer_rejected"
    return {"collected": [node], "deselected": [], "incomplete": False, "exitstatus": 0,
            "metrics": [], "results": [{"nodeid": node, "when": phase,
                "outcome": outcome if phase == "call" else "passed", "xfail": xfail,
                "failure": ""} for phase in ("setup", "call", "teardown")]}


@pytest.mark.parametrize("mode", ["zero", "deselected", "skipped", "xfail", "missing_teardown"])
def test_joined_gate_refuses_incomplete_acceptance(mode):
    report = report_for()
    if mode == "zero":
        report["collected"] = []
    elif mode == "deselected":
        report["deselected"] = ["omitted"]
    elif mode == "skipped":
        report["results"][1]["outcome"] = "skipped"
    elif mode == "xfail":
        report["results"][1]["xfail"] = True
    else:
        report["results"].pop()
    with pytest.raises(support.HarnessError):
        runner.validate_joined(report)


@pytest.mark.parametrize("proof", [False, True])
def test_self_check_requires_backend_execution_evidence(proof):
    report = report_for("failed")
    report["results"][1]["failure"] = "SELF_CHECK_BACKEND_REACHED"
    report["metrics"] = [{"fault": "SELF_CHECK_BACKEND_REACHED", "status": 200,
                          "custom_backend_calls": 1 if proof else 0}]
    if proof:
        runner.validate_joined(report, self_check=True)
    else:
        with pytest.raises(support.HarnessError, match="unrelated reason"):
            runner.validate_joined(report, self_check=True)


def test_joined_plugin_turns_skip_into_failure_and_reports_it(tmp_path):
    destination = tmp_path / "joined.json"
    options = {"joined_report": str(destination)}
    config = SimpleNamespace(_joined_metrics=[], getoption=lambda name: options[name])
    recorder = JoinedResults(config)
    recorder.collected = ["case"]
    recorder.reports = [{"nodeid": "case", "when": "setup", "outcome": "skipped", "xfail": False, "failure": ""}]
    session = SimpleNamespace(exitstatus=pytest.ExitCode.OK)
    recorder.pytest_sessionfinish(session, pytest.ExitCode.OK)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    with pytest.raises(support.HarnessError):
        runner.validate_joined(json.loads(destination.read_text()))


def test_timeout_terminates_owned_process_group():
    with pytest.raises(support.HarnessError, match="timed out"):
        support.run_command([sys.executable, "-c", "import time; time.sleep(60)"], cwd=support.PROJECT, timeout=0.1)
