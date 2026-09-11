"""Bounded, offline harness operations shared by joined tests and the gate."""

import json
import os
from pathlib import Path
import platform
import re
import selectors
import shutil
import signal
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parents[1]
JOINED_MODULE = PROJECT / "integration" / "coda_neuprint_check.py"
SELF_CHECK_TEST = "test_invalid_bearer_rejected"


class HarnessError(RuntimeError):
    pass


def redact(text):
    text = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", str(text))
    return re.sub(r"\b[0-9a-fA-F]{64}\b", "[REDACTED-64-HEX]", text)


def offline_go_env():
    return {**os.environ, "GOTOOLCHAIN": "local", "GOPROXY": "off", "GOSUMDB": "off"}


class LockGuard:
    def __init__(self, repo):
        self.paths = [PROJECT / "pixi.lock", repo / "go.mod", repo / "go.sum"]
        self.original = {path: path.read_bytes() for path in self.paths}

    def check(self):
        for path, original in self.original.items():
            if not path.is_file() or path.read_bytes() != original:
                raise HarnessError(f"Protected dependency file changed: {path}")


def terminate(process, *, group=True):
    if process.poll() is not None:
        return
    try:
        if group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if group:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=3)
    except ProcessLookupError:
        process.wait(timeout=3)


def run_command(args, *, cwd, timeout=180, env=None):
    """Own and reap the entire subprocess group, including interrupted compilers."""
    try:
        process = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, start_new_session=True)
    except OSError as exc:
        raise HarnessError(f"Cannot start {Path(args[0]).name}: {exc}") from exc
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        terminate(process)
        raise HarnessError(f"{Path(args[0]).name} timed out after {timeout}s") from exc
    except BaseException:
        terminate(process)
        raise
    return process.returncode, redact(output)


def host_go_platform():
    systems = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows", "FreeBSD": "freebsd"}
    machines = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64",
                "i386": "386", "i686": "386", "armv7l": "arm"}
    try:
        return systems[platform.system()], machines[platform.machine().lower()]
    except KeyError as exc:
        raise HarnessError("Unsupported host platform for cached Go toolchain discovery; pass --go") from exc


def resolve_go(repo, explicit=None):
    override = explicit or os.environ.get("GO")
    if override:
        candidates = [Path(shutil.which(override) or override).expanduser().resolve()]
    else:
        candidates = []
        on_path = shutil.which("go")
        if on_path:
            candidates.append(Path(on_path).resolve())
        version = re.search(r"^toolchain\s+(go[\d.]+)\s*$", (repo / "go.mod").read_text(), re.MULTILINE)
        if version:
            goos, goarch = host_go_platform()
            gopath = Path(os.environ.get("GOPATH", str(Path.home() / "go")).split(os.pathsep)[0])
            cache = Path(os.environ.get("GOMODCACHE", str(gopath / "pkg" / "mod")))
            candidates.append(cache / f"golang.org/toolchain@v0.0.1-{version[1]}.{goos}-{goarch}" / "bin" / ("go.exe" if goos == "windows" else "go"))
    failures = []
    for candidate in candidates:
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            failures.append(f"not executable: {candidate}")
            continue
        try:
            code, output = run_command([str(candidate), "version"], cwd=repo, timeout=10, env=offline_go_env())
        except HarnessError as exc:
            failures.append(str(exc))
            continue
        if code == 0 and output.startswith("go version go"):
            return candidate, output.strip()
        failures.append(f"version check failed: {candidate}: {output.strip()}")
    raise HarnessError("Go toolchain unavailable (no installs attempted): " + "; ".join(failures))


def resolve_repo(value):
    if not value:
        raise HarnessError("--neuprint-repo is required for joined tests")
    repo = Path(value).expanduser().resolve()
    required = ["go.mod", "go.sum", "main.go", "predeploy_driver_test.go"]
    if not repo.is_dir() or any(not (repo / name).is_file() for name in required):
        raise HarnessError(f"neuPrintHTTP checkout or predeploy driver missing: {repo}")
    return repo


def require_gpg():
    gpg = shutil.which("gpg")
    if not gpg:
        raise HarnessError("Required executable missing: gpg")
    gpgconf = Path(gpg).resolve().with_name("gpgconf")
    if not gpgconf.is_file() or not os.access(gpgconf, os.X_OK):
        raise HarnessError(f"Required matching executable missing: gpgconf ({gpgconf})")
    return Path(gpg), gpgconf


def build_driver(repo, go, destination):
    guard = LockGuard(repo)
    try:
        code, output = run_command([str(go), "test", "-mod=readonly", "-c", "-o", str(destination), "."],
                                   cwd=repo, env=offline_go_env())
    finally:
        guard.check()
    if code:
        raise HarnessError(f"Go test driver compilation failed (exit {code}):\n{output}")
    return destination


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HarnessError(f"Unexpected HTTP redirect: {code}")


class Driver:
    def __init__(self, binary, repo, workdir, dsg_url, datasets, *, disable_auth=False):
        self.process = None
        self.stderr = None
        self.address = None
        self.opener = None
        self.startup_output = ""
        cert = workdir / "loopback.pem"
        config = workdir / "driver.json"
        config.write_text(json.dumps({"dsg-url": dsg_url, "dsg-cache-ttl": 1,
                                      "dsg-service-name": "neuprint", "hostname": "127.0.0.1",
                                      "disable-auth": disable_auth}))
        args = [str(binary), "-test.run=^TestPredeployDriver$", "-test.timeout=120s",
                "-predeploy-config=" + str(config), "-predeploy-cert=" + str(cert),
                "-predeploy-datasets=" + ",".join(datasets)]
        try:
            self.stderr = (workdir / "driver-stderr.log").open("w+")
            self.process = subprocess.Popen(args, cwd=repo, env=offline_go_env(), stdout=subprocess.PIPE,
                                            stderr=self.stderr)
            line = self._startup_line(timeout=15)
            ready = json.loads(line)
            parsed = urlsplit(ready["address"])
            if parsed.scheme != "https" or parsed.hostname != "127.0.0.1" or not parsed.port or ready["ttl_seconds"] != 1:
                raise HarnessError("Driver advertised an invalid address or TTL")
            self.address = ready["address"]
            context = ssl.create_default_context(cafile=str(cert))
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                      urllib.request.HTTPSHandler(context=context), NoRedirect())
            state = self.state()
            if state["ttl_seconds"] != 1 or any(q["accessor"] == "GetDataset" for q in state["queries"]):
                raise HarnessError("Driver did not start with fresh counters and TTL 1")
        except BaseException as exc:
            if self.process is not None:
                terminate(self.process, group=False)
            diagnostic = self.diagnostics()
            self.close()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise HarnessError(f"Driver startup failed: {redact(exc)}\n{diagnostic}") from None

    def _startup_line(self, timeout):
        deadline = time.monotonic() + timeout
        data = b""
        received = 0
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(self.process.stdout.fileno(), 4096)
                if not chunk:
                    raise HarnessError(f"Driver exited before readiness (exit {self.process.poll()})")
                data += chunk
                received += len(chunk)
                if received > 65536:
                    raise HarnessError("Driver startup output exceeded its bound")
                while b"\n" in data:
                    line, data = data.split(b"\n", 1)
                    if line.startswith(b"{"):
                        return line.decode()
                    # Backend package init can log before the driver's test
                    # function starts. Keep a bounded prelude for diagnostics.
                    self.startup_output += line.decode(errors="replace") + "\n"
        raise HarnessError(f"Driver readiness timed out after {timeout}s")

    def diagnostics(self):
        if self.stderr is None:
            return ""
        self.stderr.flush()
        self.stderr.seek(0)
        return redact(self.startup_output + self.stderr.read()[-12000:])

    def request(self, path, *, bearer=None, data=None, timeout=3):
        headers = {}
        if bearer is not None:
            headers["Authorization"] = "Bearer " + bearer
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.address + path, headers=headers,
                                     data=None if data is None else json.dumps(data).encode())
        try:
            response = self.opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        except (OSError, urllib.error.URLError) as exc:
            raise HarnessError(f"Loopback request failed: {redact(exc)}") from None
        with response:
            status = response.status
            if 300 <= status < 400:
                raise HarnessError(f"Unexpected HTTP redirect: {status}")
            body = response.read()
            try:
                decoded = json.loads(body)
            except ValueError:
                raise HarnessError(f"Loopback response was not JSON (status {status})") from None
            return status, decoded, dict(response.headers)

    def state(self, *, timeout=3):
        status, data, _ = self.request("/__predeploy/state", timeout=timeout)
        if status != 200:
            raise HarnessError(f"Driver state endpoint returned {status}")
        return data

    def close(self):
        if self.process is not None:
            terminate(self.process, group=False)
            if self.process.stdout:
                self.process.stdout.close()
        if self.stderr is not None:
            self.stderr.close()
