#!/usr/bin/env python3
"""Interactive setup wizard for DatasetGateway.

Gathers settings interactively and generates a .env file. If .env already
exists, its values are used as defaults so you can re-run to update.

Usage:
    pixi run setup
"""

import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


# =============================================================================
# Utility Functions
# =============================================================================


def prompt(message: str, default: str = "") -> str:
    """Prompt user for input with optional default."""
    if default:
        user_input = input(f"  {message} [{default}]: ").strip()
        return user_input if user_input else default
    else:
        while True:
            user_input = input(f"  {message}: ").strip()
            if user_input:
                return user_input
            print("    This field is required.")


def prompt_yes_no(message: str, default: bool = True) -> bool:
    """Prompt user for yes/no with default."""
    default_str = "Y/n" if default else "y/N"
    while True:
        response = input(f"  {message} [{default_str}]: ").strip().lower()
        if not response:
            return default
        if response in ("y", "yes"):
            return True
        if response in ("n", "no"):
            return False
        print("    Please enter 'y' or 'n'")


def prompt_optional(message: str, default: str = "") -> str:
    """Prompt user for optional input."""
    if default:
        user_input = input(f"  {message} [{default}]: ").strip()
        return user_input if user_input else default
    else:
        return input(f"  {message} (optional, press Enter to skip): ").strip()


def load_dotenv(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict, ignoring comments and blank lines."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def write_dotenv(path: Path, values: dict[str, str]) -> None:
    """Write a dict to a .env file."""
    lines = []
    for key, value in values.items():
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")


# =============================================================================
# OAuth Credential Check
# =============================================================================


def check_oauth_credentials(project_root: Path) -> bool:
    """Check for client_credentials.json; if missing, print setup steps."""
    creds_path = project_root / "secrets" / "client_credentials.json"

    if creds_path.exists():
        print(f"\n  Found: {creds_path.relative_to(project_root)}")
        return True

    print("\n" + "=" * 60)
    print("Google OAuth Credentials")
    print("=" * 60)
    print("\n  Login requires a Google OAuth 2.0 client. To create one:")
    print()
    print("  1. Go to the GCP Console Credentials page:")
    print("     https://console.cloud.google.com/apis/credentials")
    print("  2. Click 'Create Credentials' > 'OAuth client ID'")
    print("  3. Application type: 'Web application'")
    print("  4. Name: 'DatasetGateway' (or any name you like)")
    print("  5. Add Authorized redirect URIs:")
    print("     - http://localhost:8200/accounts/google/login/callback/")
    print("     - (add your production URI too, if known)")
    print("  6. Click 'Create', then 'Download JSON'")
    print(f"  7. Save the file as: {creds_path}")
    print()
    print("  The secrets/ directory is gitignored.")
    print("  Alternatively, set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET")
    print("  environment variables instead of using the JSON file.")
    print()

    input("  Press Enter to continue (you can add credentials later)...")
    return creds_path.exists()


# =============================================================================
# Main Setup Flow
# =============================================================================


def main() -> None:
    # Resolve project root (dsg/)
    project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / ".env"

    print("=" * 60)
    print("DatasetGateway — Setup Wizard")
    print("=" * 60)

    # Load existing .env values as defaults
    existing = load_dotenv(env_path)
    if existing:
        print("\nFound existing .env — values will be used as defaults.")
    print("\nPress Enter to accept defaults shown in [brackets].\n")

    # --- Collect settings ---

    print("-- Core Settings --")

    dsg_origin = prompt(
        "Public origin (e.g., https://dataset-gateway.mydomain.org)",
        existing.get("DSG_ORIGIN", ""),
    )

    dsg_port = prompt(
        "Development server port",
        existing.get("DSG_PORT", "8200"),
    )

    # --- Secret key ---
    print("\n-- Security --")

    existing_key = existing.get("DJANGO_SECRET_KEY", "")
    if existing_key:
        secret_key = prompt("Django secret key", existing_key)
    else:
        generated_key = secrets.token_urlsafe(50)
        print(f"  Generated secret key: {generated_key}")
        if prompt_yes_no("Accept this key?", default=True):
            secret_key = generated_key
        else:
            secret_key = prompt("Django secret key")

    # --- Allowed hosts ---
    parsed = urlparse(dsg_origin)
    derived_host = parsed.hostname or ""
    default_hosts = existing.get("DJANGO_ALLOWED_HOSTS", derived_host)

    allowed_hosts = prompt(
        "Allowed hosts (comma-separated)",
        default_hosts,
    )

    # --- Debug mode ---
    existing_debug = existing.get("DJANGO_DEBUG", "True")
    debug = prompt_yes_no(
        "Enable Django debug mode?",
        default=existing_debug.lower() in ("true", "1", "yes"),
    )

    # --- SSL redirect ---
    # Almost always False — most deployments sit behind nginx/caddy for TLS.
    # Only set True if Django is directly exposed to the internet with TLS.
    existing_ssl = existing.get("SECURE_SSL_REDIRECT", "False")
    ssl_redirect = existing_ssl.lower() in ("true", "1", "yes")

    # --- Cookie domain ---
    # Derive default from origin: https://auth.janelia.org → .janelia.org
    existing_cookie = existing.get("AUTH_COOKIE_DOMAIN", "")
    if existing_cookie:
        default_cookie = existing_cookie
    elif parsed.hostname and "." in parsed.hostname:
        # Strip the first subdomain: auth.janelia.org → .janelia.org
        parts = parsed.hostname.split(".", 1)
        default_cookie = f".{parts[1]}" if len(parts) > 1 else ""
    else:
        default_cookie = ""

    print("\n-- Cookie Domain --")
    print("  If other services (neuPrint, CAVE, etc.) run on sibling")
    print("  subdomains, set this so they can share the login cookie.")
    print("  Example: .janelia.org lets *.janelia.org share cookies.")
    print("  Leave blank for local development.\n")

    auth_cookie_domain = prompt_optional(
        "Cookie domain",
        default_cookie,
    )

    # --- Build env dict ---
    env = {
        "DSG_ORIGIN": dsg_origin,
        "DSG_PORT": dsg_port,
        "DJANGO_SECRET_KEY": secret_key,
        "DJANGO_ALLOWED_HOSTS": allowed_hosts,
        "DJANGO_DEBUG": str(debug),
        "SECURE_SSL_REDIRECT": str(ssl_redirect),
    }
    if auth_cookie_domain:
        env["AUTH_COOKIE_DOMAIN"] = auth_cookie_domain

    # Preserve any extra keys from the existing .env that we didn't prompt for
    for key, value in existing.items():
        if key not in env:
            env[key] = value

    # --- Write .env ---
    write_dotenv(env_path, env)
    print(f"\n  Saved: {env_path.relative_to(project_root)}")

    # --- Check OAuth credentials ---
    check_oauth_credentials(project_root)

    # --- Database setup ---
    print("\n" + "=" * 60)
    print("Database Setup")
    print("=" * 60)

    manage_py = project_root / "manage.py"
    for cmd, desc in [
        (["python", str(manage_py), "migrate", "--noinput"], "Running migrations"),
        (["python", str(manage_py), "seed_permissions"], "Seeding permissions"),
        (["python", str(manage_py), "seed_groups"], "Seeding groups"),
    ]:
        print(f"\n  {desc}...")
        result = subprocess.run(cmd, cwd=project_root)
        if result.returncode != 0:
            print(f"  Warning: '{' '.join(cmd[-2:])}' exited with code {result.returncode}")

    # --- Summary ---
    print("\n" + "=" * 60)
    print("Setup Complete!")
    print("=" * 60)
    print(f"""
  Origin:          {dsg_origin}
  Port:            {dsg_port}
  Debug:           {debug}
  Allowed hosts:   {allowed_hosts}

Next steps:

  Local development:
    pixi run serve

  Docker production:
    pixi run deploy
""")


if __name__ == "__main__":
    main()
