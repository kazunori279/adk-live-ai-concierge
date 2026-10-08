"""Running the `gws` binary.

Split out of `gws.py` so that `gws_cache` can make the two small calls it needs
— a Drive `files get` for a title and a `modifiedTime` — without importing the
module that imports it.
"""

import json
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

# gws talks to the Workspace APIs over the network, and a Drive listing on a
# cold OAuth token is not instant. Still short enough that a wedged call gives
# up while the person is arguably still waiting.
TIMEOUT = 45


def binary() -> str:
    """Absolute path to the gws binary, so no PATH lookup happens per call."""
    return os.getenv("GWS_BIN") or shutil.which("gws") or "gws"


def available() -> bool:
    """Is there a `gws` on this machine to run at all?

    :func:`binary` falls back to the bare name when nothing is found, so a
    terminal without the CLI learns about it from a FileNotFoundError several
    calls into a question rather than before the first one, and what it learns
    is a tool result the sub-agent is free to retry.

    Asked per question rather than cached at startup: the lookup is
    microseconds against a question that costs seconds, and paying it every
    time is what makes a `gws` installed while the server is up work on the
    next question instead of after a restart.

    `shutil.which` and not `os.access`, because `GWS_BIN` may be a bare name.
    :func:`binary` hands one straight to `subprocess`, which resolves it
    through PATH, while `os.access` resolves it against the working directory —
    so the two disagreed about a `gws` that runs perfectly well and every
    question answered that the CLI was not installed. `which` tests a value
    with a separator in it as a path and skips PATH, which is the behaviour an
    absolute `GWS_BIN` had before, and it rejects a directory, which
    `os.access` accepts because the execute bit on one means searchable.
    """
    return shutil.which(os.getenv("GWS_BIN") or "gws") is not None


class GwsError(RuntimeError):
    """A gws command that did not come back with a JSON object."""


class GwsAuthExpiredError(GwsError):
    """A gws command failed because Google Workspace OAuth access expired or was revoked."""


AUTH_EXPIRED_PATTERNS = (
    "invalid_grant",
    "token has been expired or revoked",
    "unauthorized",
    "re-authenticate",
)


def is_auth_expired(stdout: str = "", stderr: str = "") -> bool:
    """Whether stdout/stderr indicates an expired or revoked OAuth credential."""
    combined = f"{stdout}\n{stderr}".lower()
    if any(p in combined for p in AUTH_EXPIRED_PATTERNS):
        return True
    if "401" in combined and any(
        k in combined for k in ("oauth", "auth", "unauthorized", "unauthenticated", "status", "error", "http")
    ):
        return True
    return False


def run_json(argv: list[str], cwd=None, timeout: float = TIMEOUT) -> dict:
    """Run a gws command and return the JSON object it printed.

    `timeout` is a float because `_agenda` passes what is left of a deadline
    shared by seven parallel calls, rather than a constant.

    Raises :class:`GwsError` for everything else — a missing binary, a timeout,
    a non-zero exit, output that is not JSON — carrying whatever the command
    said about why, for a caller that has something to do with it.

    gws prints a line about its keyring backend before the payload, and a
    warning has been seen on stdout too, so the parse starts at the first brace
    rather than at the first byte. No brace at all means there is no payload:
    that is a failure and not an empty answer, and the two are worth telling
    apart — a calendar reporting zero events and a calendar that could not be
    read are different things to say out loud.
    """
    try:
        proc = subprocess.run(
            [binary(), *argv], cwd=cwd, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise GwsError(str(err)) from err

    if is_auth_expired(proc.stdout, proc.stderr):
        raise GwsAuthExpiredError(proc.stderr.strip()[:400] or proc.stdout.strip()[:400] or "auth expired")

    start = proc.stdout.find("{")
    if proc.returncode != 0:
        raise GwsError(proc.stderr.strip()[:400] or f"exit {proc.returncode}")
    if start < 0:
        raise GwsError(proc.stdout.strip()[:200] or "no output")
    try:
        parsed = json.loads(proc.stdout[start:])
    except ValueError as err:
        raise GwsError(f"unparseable output: {err}") from err
    if not isinstance(parsed, dict):
        raise GwsError(f"expected an object, got {type(parsed).__name__}")
    if is_auth_expired(json.dumps(parsed)):
        raise GwsAuthExpiredError(proc.stderr.strip()[:400] or proc.stdout.strip()[:400] or "auth expired")
    return parsed


def json_output(argv: list[str], cwd, timeout: float = TIMEOUT) -> dict | None:
    """:func:`run_json`, with None for anything that did not work.

    Its one caller is enriching a cache entry and has nothing useful to do
    with the reason it failed.
    """
    try:
        return run_json(argv, cwd, timeout)
    except GwsError as err:
        logger.warning("gws %s: %s", " ".join(argv[:3]), err)
        return None
