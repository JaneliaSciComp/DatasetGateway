#!/usr/bin/env python3
"""Offline pre-deployment gate. No serving configuration or live accounts."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from integration.support import (HarnessError, JOINED_MODULE, LockGuard, SELF_CHECK_TEST,
                                 offline_go_env, redact, require_gpg, resolve_go,
                                 resolve_repo, run_command)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--neuprint-repo", required=True, help="Sibling neuPrintHTTP checkout; resolved before running commands.")
    result.add_argument("--go", help="Existing Go executable (then $GO, PATH, matching module-cache toolchain).")
    result.add_argument("--self-check", action="store_true", help="Inject disable-auth and require the invalid-Bearer query to reach the backend; expected exit 1 when detected.")
    result.add_argument("--timeout", type=float, default=300, help="Maximum seconds per subprocess phase (default: 300).")
    result.add_argument("--output-dir", default=str(PROJECT / ".pytest_cache" / "predeploy"), help="Directory for sanitized phase logs and joined metrics.")
    result.add_argument("--joined-file-db", action="store_true", help="Use disposable file-backed SQLite when investigating shared-cache locking.")
    return result


def revision(repo):
    code, head = run_command(["git", "rev-parse", "HEAD"], cwd=repo, timeout=10)
    if code:
        raise HarnessError(f"Cannot determine repository HEAD: {repo}")
    code, dirty = run_command(["git", "status", "--porcelain"], cwd=repo, timeout=10)
    if code:
        raise HarnessError(f"Cannot determine repository dirty state: {repo}")
    return {"head": head.strip(), "dirty": bool(dirty.strip())}


def validate_joined(report, *, self_check=False):
    collected = report.get("collected", [])
    results = report.get("results", [])
    if not collected or report.get("incomplete", True) or report.get("deselected"):
        raise HarnessError("Joined gate incomplete: zero, deselected, skipped or unexecuted cases")
    if any(row["outcome"] == "skipped" or row["xfail"] for row in results):
        raise HarnessError("Joined gate refuses skipped or xfail cases")
    for node in collected:
        phases = [row for row in results if row["nodeid"] == node]
        if sorted(row["when"] for row in phases) != ["call", "setup", "teardown"]:
            raise HarnessError("Joined gate did not report every test phase exactly once")
    if self_check:
        expected_node = "integration/coda_neuprint_check.py::" + SELF_CHECK_TEST
        proof = [row for row in report.get("metrics", []) if row.get("fault") == "SELF_CHECK_BACKEND_REACHED"
                 and row.get("status") == 200 and row.get("custom_backend_calls", 0) > 0]
        failures = [row for row in results if row["outcome"] != "passed"]
        if (collected != [expected_node] or len(proof) != 1 or len(failures) != 1
                or failures[0]["when"] != "call" or failures[0]["outcome"] != "failed"
                or "SELF_CHECK_BACKEND_REACHED" not in failures[0]["failure"]):
            raise HarnessError("Self-check failed for an unrelated reason; no verified backend-execution fault")
    elif any(row["outcome"] != "passed" for row in results) or report.get("exitstatus") != 0:
        raise HarnessError("Joined gate has failing cases")


def execute(args):
    if args.timeout <= 0:
        raise HarnessError("--timeout must be positive")
    repo = resolve_repo(args.neuprint_repo)
    output_dir = Path(args.output_dir).expanduser().resolve()
    require_gpg()
    guard = LockGuard(repo)
    try:
        go, version = resolve_go(repo, args.go)
        guard.check()
        revisions = {"DatasetGateway": revision(PROJECT.parent), "neuPrintHTTP": revision(repo)}
        for name, details in revisions.items():
            print(f"{name}: HEAD={details['head']} dirty={str(details['dirty']).lower()}", flush=True)
        print(f"Go: {go} ({version})", flush=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "revisions.json").write_text(json.dumps(revisions, indent=2) + "\n")
        with tempfile.TemporaryDirectory(prefix="dsg-predeploy-") as workdir:
            env = {**offline_go_env(), "PYTEST_ADDOPTS": "", "PYTEST_PLUGINS": "",
                   "DJANGO_SETTINGS_MODULE": "dsg.settings", "DJANGO_DEBUG": "True",
                   "DJANGO_SECRET_KEY": "predeploy-test-secret-not-for-serving",
                   "DJANGO_ALLOWED_HOSTS": "*", "AUTH_COOKIE_DOMAIN": "", "DSG_ORIGIN": "",
                   "GOOGLE_CLIENT_ID": "predeploy-test", "GOOGLE_CLIENT_SECRET": "predeploy-test",
                   "DATABASE_PATH": str(Path(workdir) / "unused.sqlite3")}

            def phase(name, command, cwd):
                print(f"Phase: {name}", flush=True)
                try:
                    code, output = run_command(command, cwd=cwd, env=env, timeout=args.timeout)
                finally:
                    guard.check()
                (output_dir / (name + ".log")).write_text(output)
                print(output, end="" if output.endswith("\n") else "\n", flush=True)
                return code

            if not args.self_check:
                code = phase("dsg", [sys.executable, "-m", "pytest", "-q", "--tb=short", "--show-capture=no",
                                     "--disable-warnings", "--observe-routes",
                                     "--route-observations=" + str(output_dir / "routes.json")], PROJECT)
                if code:
                    raise HarnessError(f"DSG suite failed (exit {code}); see {output_dir / 'dsg.log'}")
                code = phase("go", [str(go), "test", "-mod=readonly", "-count=1", "./..."], repo)
                if code:
                    raise HarnessError(f"Go suite failed (exit {code}); see {output_dir / 'go.log'}")
            report_path = output_dir / ("self-check.json" if args.self_check else "joined.json")
            report_path.unlink(missing_ok=True)
            selected = str(JOINED_MODULE)
            if args.self_check:
                selected += "::" + SELF_CHECK_TEST
            command = [sys.executable, "-m", "pytest", "-q", "--tb=short", "--show-capture=no", "--disable-warnings",
                       "-p", "integration.pytest_plugin", "--run-joined", selected,
                       "--neuprint-repo", str(repo), "--go", str(go), "--joined-report", str(report_path)]
            if args.self_check:
                command.append("--joined-self-check")
            if args.joined_file_db:
                command.append("--joined-file-db")
            code = phase("self-check" if args.self_check else "joined", command, PROJECT)
            if not report_path.is_file():
                raise HarnessError(f"Joined process produced no result report (exit {code})")
            try:
                report = json.loads(report_path.read_text())
            except (OSError, ValueError) as exc:
                raise HarnessError("Joined process produced an invalid result report") from exc
            validate_joined(report, self_check=args.self_check)
            if args.self_check:
                if code != 1:
                    raise HarnessError(f"Self-check returned unexpected exit {code}")
                print("SELF-CHECK DETECTED: invalid Bearer query succeeded and reached the backend (expected exit 1).")
                return 1
            if code:
                raise HarnessError(f"Joined process failed (exit {code})")
            print(f"Pre-deployment gate passed. Reports: {output_dir}")
            return 0
    finally:
        guard.check()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return execute(args)
    except HarnessError as exc:
        print("Pre-deployment gate failed: " + redact(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Pre-deployment gate interrupted; subprocess groups terminated.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
