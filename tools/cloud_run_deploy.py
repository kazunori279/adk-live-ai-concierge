#!/usr/bin/env python3
"""Deploy the AI Terminal demo server to Google Cloud Run.

Provides validation and deployment tooling for containerized live-server:
    uv run python tools/cloud_run_deploy.py check
    uv run python tools/cloud_run_deploy.py deploy --dry-run
    uv run python tools/cloud_run_deploy.py deploy --project my-project --region us-central1
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"

DEFAULT_SERVICE = "ai-terminal-demo"
DEFAULT_REGION = "us-central1"
DEFAULT_ALLOWED_HOSTS = (
    "ai-terminal-demo-example-ue.a.run.app,"
    "ai-terminal-demo-000000000000.us-east1.run.app"
)
DEFAULT_ENV_VARS = (
    "SESSIONS_DB=/tmp/sessions.db,"
    "GWS_CACHE_DIR=/tmp/gws_cache,"
    "SCREENSAVER_DIR=/tmp/screensaver,"
    f'ALLOWED_HOSTS="{DEFAULT_ALLOWED_HOSTS}"'
)


def build_deploy_command(
    service: str = DEFAULT_SERVICE,
    project: str | None = None,
    region: str | None = DEFAULT_REGION,
    dry_run: bool = False,
) -> list[str]:
    """Assemble the gcloud run deploy command list."""
    cmd = [
        "gcloud", "run", "deploy", service,
        "--source", ".",
        "--timeout=3600",
        "--session-affinity",
        "--allow-unauthenticated",
        "--set-env-vars", DEFAULT_ENV_VARS,
    ]
    if region:
        cmd.extend(["--region", region])
    if project:
        cmd.extend(["--project", project])
    return cmd


def check_prerequisites() -> list[tuple[str, bool, str]]:
    """Verify local Dockerfile, .dockerignore, and environment readiness."""
    results: list[tuple[str, bool, str]] = []

    # 1. Dockerfile
    if DOCKERFILE.exists():
        content = DOCKERFILE.read_text(encoding="utf-8")
        has_base = "python:3.13-slim" in content
        has_uv = "ghcr.io/astral-sh/uv" in content
        has_portaudio = "libportaudio2" in content
        has_cmd = "live-server" in content
        has_gws = "googleworkspace/cli" in content
        has_playwright = "playwright install --with-deps chromium" in content
        ok = has_base and has_uv and has_portaudio and has_cmd and has_gws and has_playwright
        results.append(("Dockerfile exists and contains required directives", ok, DOCKERFILE.name))
    else:
        results.append(("Dockerfile exists and contains required directives", False, "missing"))

    # 2. .dockerignore
    if DOCKERIGNORE.exists():
        ign_content = DOCKERIGNORE.read_text(encoding="utf-8")
        patterns = [p.strip() for p in ign_content.splitlines() if p.strip() and not p.startswith("#")]
        essential = [".git", ".venv", ".env*"]
        ok = all(any(p == e or e in p for p in patterns) for e in essential)
        results.append((".dockerignore exists with required ignores", ok, f"{len(patterns)} patterns"))
    else:
        results.append((".dockerignore exists with required ignores", False, "missing"))

    # 3. gcloud CLI presence
    gcloud_path = shutil.which("gcloud")
    results.append(("gcloud CLI installed and available on PATH", gcloud_path is not None, gcloud_path or "not found"))

    return results


def run_check() -> int:
    """Run check phase and report status."""
    print("Checking Cloud Run deployment prerequisites:")
    all_ok = True
    for desc, ok, detail in check_prerequisites():
        status = "ok  " if ok else "WARN" if "gcloud" in desc else "FAIL"
        if not ok and "gcloud" not in desc:
            all_ok = False
        print(f"   {status} {desc}: {detail}")
    return 0 if all_ok else 1


def run_deploy(args: argparse.Namespace) -> int:
    """Run deploy phase (or dry-run)."""
    cmd = build_deploy_command(
        service=args.service,
        project=args.project,
        region=args.region,
        dry_run=args.dry_run,
    )
    cmd_str = " ".join(cmd)
    if args.dry_run:
        print(f"[dry-run] {cmd_str}")
        return 0

    print(f"Executing: {cmd_str}")
    res = subprocess.run(cmd, cwd=str(ROOT))
    return res.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        default="deploy",
        choices=["check", "deploy"],
        help="Action to run (default: deploy)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print deploy command without executing")
    parser.add_argument(
        "--project",
        default=os.getenv("CLOUD_RUN_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT"),
        help="Google Cloud project ID",
    )
    parser.add_argument(
        "--region",
        default=os.getenv("CLOUD_RUN_REGION", DEFAULT_REGION),
        help=f"Cloud Run region (default: {DEFAULT_REGION})",
    )
    parser.add_argument(
        "--service",
        default=os.getenv("CLOUD_RUN_SERVICE", DEFAULT_SERVICE),
        help=f"Cloud Run service name (default: {DEFAULT_SERVICE})",
    )

    args = parser.parse_args(argv)

    if args.command == "check":
        return run_check()
    elif args.command == "deploy":
        return run_deploy(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
