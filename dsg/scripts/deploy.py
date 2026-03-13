#!/usr/bin/env python3
"""Docker deployment script for DatasetGateway.

Validates prerequisites, builds the Docker image, starts the container,
and runs seed commands.

Usage:
    pixi run deploy
"""

import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Run a command, printing it first."""
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, **kwargs)


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / ".env"
    compose_file = project_root / "docker-compose.yml"

    print("=" * 60)
    print("DatasetGateway — Docker Deployment")
    print("=" * 60)

    # --- Check .env ---
    if not env_path.exists():
        print("\nNo .env file found — running setup wizard first.\n")
        result = run(
            [sys.executable, str(project_root / "scripts" / "setup.py")],
            check=False,
        )
        if result.returncode != 0:
            print("Setup failed.")
            sys.exit(1)
        if not env_path.exists():
            print("Error: .env was not created.")
            sys.exit(1)
    else:
        print(f"\n  Using existing .env")

    # --- Check Docker ---
    print("\n[1/4] Checking prerequisites...")

    if not shutil.which("docker"):
        print("  Error: Docker is not installed or not on PATH.")
        print("  Install from: https://docs.docker.com/get-docker/")
        sys.exit(1)
    print("  Docker: OK")

    result = run(
        ["docker", "info"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        print("  Error: Docker daemon is not running.")
        print("  Start Docker and try again.")
        sys.exit(1)
    print("  Docker daemon: OK")

    if not compose_file.exists():
        print(f"  Error: {compose_file} not found.")
        sys.exit(1)
    print("  docker-compose.yml: OK")

    creds_path = project_root / "secrets" / "client_credentials.json"
    if not creds_path.exists():
        print("  Warning: secrets/client_credentials.json not found.")
        print("  Google login will not work until credentials are added.")
    else:
        print("  OAuth credentials: OK")

    # --- Build and start ---
    print("\n[2/4] Building and starting containers...")
    run(
        ["docker", "compose", "-f", str(compose_file), "up", "-d", "--build"],
        cwd=project_root,
    )

    # --- Run seed commands ---
    print("\n[3/4] Running database migrations and seed data...")
    compose_exec = [
        "docker", "compose", "-f", str(compose_file), "exec", "dsg",
    ]
    run([*compose_exec, "python", "manage.py", "migrate", "--noinput"])
    run([*compose_exec, "python", "manage.py", "seed_permissions"])
    run([*compose_exec, "python", "manage.py", "seed_groups"])

    # --- Summary ---
    # Read origin from .env
    origin = "http://localhost:8080"
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("DSG_ORIGIN="):
            origin = line.split("=", 1)[1].strip()
            break

    cf = str(compose_file)
    print("\n[4/4] Deployment complete!")
    print("\n" + "=" * 60)
    print("DatasetGateway is running")
    print("=" * 60)
    print(f"""
  Service:     {origin}
  Admin:       {origin}/admin/

  Next steps:
    1. Create an admin user:
       docker compose -f {cf} exec dsg \\
           python manage.py make_admin user@example.com

    2. Put a reverse proxy (nginx/caddy) in front for TLS termination

  Useful commands:
    pixi run deploy                                     # rebuild and redeploy
    pixi run stop                                       # stop containers
    docker compose -f {cf} logs -f     # view logs
""")


if __name__ == "__main__":
    main()
