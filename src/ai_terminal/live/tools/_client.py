"""Shared clients and endpoints for the grounding tools.

Two access paths are needed because they expose different features:

- ``genai.Client`` for Google Maps grounding, which the SDK models directly.
- A raw ``generateContent`` POST for Google Search grounding, because
  ``enhancedContent.imageSearch`` has no SDK surface yet and the endpoint is
  project-number scoped.
"""

import functools
import os
from pathlib import Path
import platform
import shutil
import threading
from typing import Any

import google.auth
import google.auth.exceptions
import google.auth.transport.requests as auth_requests
from google import genai

from . import _gws_cli

SEARCH_ENDPOINT = (
    "https://aiplatform.googleapis.com/v1/projects/{project_number}"
    "/locations/global/publishers/google/models/{model}:generateContent"
)

# Everything a grounding branch reads is read inside the first tool call, and a
# missing one is a `KeyError` or a `DefaultCredentialsError` there, inside the
# `except Exception` that `place.py` wraps each branch in. That turns an unset
# variable into an empty branch: a thin card and no error, which is the exact
# failure this list exists to convert into a startup message.
#
# What every branch needs, whichever path is configured. Search grounding is a
# raw POST to a project-number-scoped endpoint signed with ADC — `search_url`
# and `authed_session` below — and that is the same POST in both modes, so
# neither of these belongs to :data:`VERTEX_REQUIRED`. `web_search`,
# `image_search` and `youtube_search` all go through it.
SHARED_REQUIRED = ("GOOGLE_CLOUD_PROJECT_NUMBER",)

# And what the Vertex path needs on top: `genai_client` reads both.
VERTEX_REQUIRED = ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")

# And what the page needs, which is nothing this process uses. See
# :func:`check_browser_env`.
BROWSER_REQUIRED = ("GOOGLE_MAPS_API_KEY",)

# The one requirement that is not an environment variable, so it needs a name
# to be reported under.
ADC = "application default credentials"

# The one shape of machine Playwright brings no browser for, and the reason
# `BROWSE_CHROMIUM` is not optional on the Pi. Both halves, because the
# developer's Mac is `arm64` too and Playwright serves it perfectly well —
# testing the machine alone reports a browser that works. See
# :func:`check_tool_env`.
NO_BUNDLED_CHROMIUM = ("Linux", "aarch64")


def use_vertex() -> bool:
    """Whether google-genai will go to Vertex, tested the way it tests it.

    Two variables, not one. `google/genai/_api_client.py:656-681` reads
    `GOOGLE_GENAI_USE_ENTERPRISE` and `GOOGLE_GENAI_USE_VERTEXAI`, both as
    `.lower() in ['true', '1']`, and enterprise wins outright when the two
    disagree — it warns and uses its own answer. So `GOOGLE_GENAI_USE_ENTERPRISE`
    alone sends the SDK to Vertex, and setting it to `false` beside
    `GOOGLE_GENAI_USE_VERTEXAI=true` sends it to the Gemini API.

    Set and empty is set. The SDK branches on `is not None`, so
    `GOOGLE_GENAI_USE_ENTERPRISE=` is enterprise saying no rather than
    enterprise saying nothing, and that is enough to overrule the other
    variable. Hence `os.environ.get` and an explicit `None` here too — this
    module tested `.upper() == "TRUE"` once and `=1` was enough to make the
    truth table disagree with the one thing it exists to mirror.
    """
    for name in ("GOOGLE_GENAI_USE_ENTERPRISE", "GOOGLE_GENAI_USE_VERTEXAI"):
        value = os.environ.get(name)
        if value is not None:
            return value.lower() in ("true", "1")
    return False


def check_env() -> list[str]:
    """What the configured path needs and does not have.

    Called at startup, so the answer arrives before a question does. Returns
    names rather than raising: the server is still worth running without
    grounding, and a refusal to boot would take the conversation with it.

    Only the conversation, though. `weather` reaches this module whenever the
    handshake carried no `lat`/`lng` and the place has to be geocoded
    (`main.py:625-631`, `weather.py:569`), and the Workspace sub-agent reaches
    it to summarise a document it just fetched (`gws_cache.py:1001`).

    Voice does not come through *this module*, which is not the same as needing
    nothing on this list. ADK builds its own bare `Client(**kwargs)`
    (`google/adk/models/google_llm.py:486`) and that resolves Vertex from the
    same environment: on the Vertex path with no ADC it raises
    `DefaultCredentialsError`, and with ADC but no `GOOGLE_CLOUD_PROJECT` it
    raises `ValueError: Could not resolve project`. Three of the four, by a
    path this function does not see and cannot report on.

    `GOOGLE_MAPS_API_KEY` is not on this list — see
    :func:`check_browser_env`.

    `tools/env_check.py` runs the whole truth table.
    """
    missing = [name for name in SHARED_REQUIRED if not os.getenv(name)]
    if use_vertex():
        missing += [name for name in VERTEX_REQUIRED if not os.getenv(name)]
    elif not _api_key():
        missing.append("GOOGLE_CLOUD_API_KEY")
    if not _have_adc():
        missing.append(ADC)
    return missing


def check_browser_env() -> list[str]:
    """What the page needs and does not have.

    Its own list because it is not this process's to use. Nothing here reads
    `GOOGLE_MAPS_API_KEY`; `main.py:535` hands it to the browser, which is
    where a referrer-restricted key belongs. Folding it into
    :func:`check_env` would put it behind a line saying grounding will fail,
    and grounding is fine — what fails is the map on the place card, which
    draws its blank without a word.

    Quiet in the same way, so it is reported in the same way, under a sentence
    that describes what actually goes wrong.
    """
    return [name for name in BROWSER_REQUIRED if not os.getenv(name)]


def check_tool_env() -> list[str]:
    """Which tools will refuse work on this machine.

    Tool names and not variable names, because neither answer is a variable
    being unset. `browse` needs a Chromium that exists and runs here, which on
    the Pi means `BROWSE_CHROMIUM`, and on the Mac means nothing at all — see
    :data:`NO_BUNDLED_CHROMIUM`. The Workspace sub-agent needs a `gws` to
    drive; `gws.py:880` already declines without one, but only once asked, and
    only in the words of a tool result the model is free to retry.

    Its own list rather than :func:`check_env`'s, for the reason
    :func:`check_browser_env` gives: neither of these is a grounding
    credential, and putting them behind a line saying grounding will fail
    describes the wrong failure.

    `shutil.which` rather than a bare existence test, because it is what
    `_gws_cli.available` already means by "there is one" — a value with a
    separator in it tested as a path and PATH skipped, the execute bit read,
    and a directory refused. A `BROWSE_CHROMIUM` that names an unreadable file
    or a directory is a browse that fails at launch, and `Path.exists()` calls
    both of them fine.

    But only `gws` is allowed to be a bare name. It really is resolved off
    PATH — `_gws_cli.binary()` hands it to `subprocess` — and Playwright is
    not: `browser.py:608-610` passes `BROWSE_CHROMIUM` straight to
    `executable_path`, and the driver's `resolveExecutablePath` is a filesystem
    `exists` with no PATH lookup anywhere. So a bare name that `which` resolves
    still fails at the first launch, which is the silence this function exists
    to break, and it is reported as what it is rather than as a file that is
    not there.

    Nothing is launched and nothing is run. Both branches are a PATH lookup;
    the cost of asking is microseconds, against a startup that already spends
    seconds in :func:`_have_adc`. Actually launching Chromium would answer more
    exactly and costs a browser spawn on the boot path of a kiosk.

    `tools/env_check.py` runs the table.
    """
    refusing = []
    chromium = os.getenv("BROWSE_CHROMIUM")
    if chromium and os.sep not in chromium:
        # Its own message, and not "is not an executable": `which chromium`
        # resolves, so a report saying the file is missing sends the operator
        # to look for a file that is there and reads as a broken check. What is
        # wrong is the shape of the value, so that is what it says.
        refusing.append(f"browse (BROWSE_CHROMIUM={chromium} is a name, and "
                        "Playwright needs a path to the executable)")
    elif chromium and not shutil.which(chromium):
        refusing.append(
            f"browse (BROWSE_CHROMIUM={chromium} is not an executable)")
    elif not chromium and _no_bundled_chromium():
        refusing.append("browse (no BROWSE_CHROMIUM, and Playwright ships no "
                        "arm64 Linux build)")
    elif not chromium and not _have_bundled_chromium():
        refusing.append("browse (no BROWSE_CHROMIUM, and Playwright Chromium is "
                        "not installed)")
    if not _gws_cli.available():
        # Named separately because the advice differs and the wrong one is
        # worse than none: telling somebody to set `GWS_BIN` when they have set
        # it reads as though the variable were not being read, and "not on
        # PATH" is not even true of an absolute path — `shutil.which` skips
        # PATH entirely for a value with a separator in it.
        gws_bin = os.getenv("GWS_BIN")
        refusing.append(
            f"gws (GWS_BIN={gws_bin} is not an executable)" if gws_bin
            else "gws (no gws on PATH; install it or set GWS_BIN)")
    return refusing


def _playwright_browsers_path() -> Path:
    env = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        return Path(env)
    sys_name = platform.system()
    if sys_name == "Darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    elif sys_name == "Windows":
        localappdata = os.getenv("LOCALAPPDATA")
        base = Path(localappdata) if localappdata else Path.home() / "AppData" / "Local"
        return base / "ms-playwright"
    else:
        return Path.home() / ".cache" / "ms-playwright"


def _have_bundled_chromium() -> bool:
    """Whether Playwright's bundled Chromium is installed on disk."""
    cache = _playwright_browsers_path()
    if not cache.is_dir():
        return False
    try:
        for entry in cache.iterdir():
            if entry.name.startswith("chromium-") and entry.is_dir():
                if (entry / "INSTALLATION_COMPLETE").exists():
                    return True
                if any(entry.glob("chrome*/**")):
                    return True
    except OSError:
        pass
    return False


def _no_bundled_chromium() -> bool:
    """Whether Playwright brings no browser for this machine's shape.

    A function so that :data:`NO_BUNDLED_CHROMIUM` is read per call rather than
    at import: the answer is a property of the machine, but the check that
    proves this one right has to be able to run as the other machine.
    """
    return (platform.system(), platform.machine()) == NO_BUNDLED_CHROMIUM


def _have_adc() -> bool:
    """Whether Application Default Credentials resolve at all.

    The requirement that actually broke the Pi, and the one thing on the list
    that presence of a variable cannot answer: all three variables set, `gcloud
    auth application-default login` never run, and `check_env` said nothing
    while every grounded question came back empty.

    Answering costs nothing when they are there and a great deal when they are
    not, which is the wrong way round and worth knowing about.
    `google.auth.default()` mints no token, but with no
    `GOOGLE_APPLICATION_CREDENTIALS`, no well-known file and no metadata server
    to answer, it spends its way through three retries against
    169.254.169.254 first. Measured on the Pi's shape: 8.9 s the first time and
    3 s each time after. Present, it is 3 ms.

    So `check_env` at import holds the server down for nine seconds on the one
    machine that most needs to see the message. That is a fair trade for the
    message; paying it again on every question was not, and it was — three of
    the grounding branches call :func:`authed_session` per card, and each of
    those went back to `google.auth` because `functools.cache` does not cache
    an exception. :func:`_resolve` caches the failure as a value.
    """
    return _resolve()[1] is None


def _api_key() -> str | None:
    """The key the Gemini-API path will actually use.

    `genai.Client(api_key=None)` is not "no key" — google-genai falls back to
    `GOOGLE_API_KEY` and then `GEMINI_API_KEY`, and this repo tells you to set
    the first of those for Chromium's geolocation (see the README). So a key
    can be in play that `.env` never mentions, and :func:`check_env` has to
    look where the SDK looks or it reports a problem that is not there.
    """
    return (os.getenv("GOOGLE_CLOUD_API_KEY") or os.getenv("GOOGLE_API_KEY")
            or os.getenv("GEMINI_API_KEY"))


def grounding_model() -> str:
    """Model used by the sidecar grounding calls.

    Flash-Lite keeps the extra round trip short enough to stay inside a
    spoken turn. That is the decision; which Flash-Lite is not, and this one
    sat on 2.5 for a month after the other defaults moved, while the README's
    own sub-agent benchmark was being re-run on `gemini-3.5-flash-lite`. A
    default nobody reads back is a default nobody notices, so
    `tools/models_check.py` reads it back: it fails when a newer name in the
    same family is written down anywhere this repo runs one.
    """
    return os.getenv("GROUNDING_MODEL", "gemini-3.5-flash-lite")


def search_url() -> str:
    """Return the generateContent URL for Google Search grounding."""
    return SEARCH_ENDPOINT.format(
        project_number=os.environ["GOOGLE_CLOUD_PROJECT_NUMBER"],
        model=grounding_model(),
    )


@functools.cache
def _resolve() -> tuple[object | None, Exception | None]:
    """Whatever `google.auth.default()` produced, credentials or refusal.

    Both outcomes cached, and the refusal is the reason this is not just
    `@functools.cache` on the call. `functools.cache` stores return values, so
    a raise is not a result and the next caller does the whole lookup again —
    on a machine with no credentials that is three metadata retries per
    grounding branch per question. Returning the exception instead of raising
    it makes the answer a value, which the cache can hold. See
    :func:`_have_adc` for the numbers.

    Resolved once per process in both directions, so `gcloud auth
    application-default login` after startup needs a restart to take. That was
    already true of the success direction, and startup is where the missing
    credentials are reported anyway.
    """
    try:
        return google.auth.default()[0], None
    except google.auth.exceptions.DefaultCredentialsError as err:
        return None, err


def _credentials():
    """Application Default Credentials, or the refusal :func:`_resolve` cached.

    Looking them up hits the filesystem or the metadata server, which is too
    slow to repeat inside a spoken turn. The credentials object refreshes its
    own token under a lock, so caching it is safe across threads.

    The error is rebuilt rather than re-raised: one instance raised from a
    cache would collect a frame on its `__traceback__` at every call site it
    passes through, for the life of the process.
    """
    credentials, err = _resolve()
    if err is not None:
        raise google.auth.exceptions.DefaultCredentialsError(*err.args)
    return credentials


def authed_session() -> auth_requests.AuthorizedSession:
    """Return a session signed with Application Default Credentials.

    A fresh session per call: ``requests.Session`` is not thread-safe, and
    tool calls run concurrently on the executor.
    """
    return auth_requests.AuthorizedSession(_credentials())


_client_lock = threading.Lock()
_cached_genai_client: genai.Client | None = None
_cached_global_client: genai.Client | None = None


def is_client_closed(client: Any) -> bool:
    """Return True if the genai client's underlying HTTP transport is closed."""
    if client is None:
        return False
    api_client = getattr(client, "_api_client", None)
    if api_client is None:
        return False
    httpx_client = getattr(api_client, "_httpx_client", None)
    if httpx_client is not None:
        return bool(getattr(httpx_client, "is_closed", False))
    async_client = getattr(api_client, "_async_httpx_client", None)
    if async_client is not None:
        return bool(getattr(async_client, "is_closed", False))
    return False


def clear_client_cache() -> None:
    """Clear cached genai and global clients so fresh ones are built on next call."""
    global _cached_genai_client, _cached_global_client
    with _client_lock:
        _cached_genai_client = None
        _cached_global_client = None


def evict_if_closed(client: Any = None) -> bool:
    """Evict closed client(s) from the cache.

    If `client` is provided and is closed, or if no client is provided and
    any cached client is closed, the closed client is cleared from the cache.
    Returns True if any cached client was cleared.
    """
    global _cached_genai_client, _cached_global_client
    with _client_lock:
        cleared = False
        if client is not None:
            if is_client_closed(client):
                if _cached_genai_client is client or is_client_closed(_cached_genai_client):
                    _cached_genai_client = None
                    cleared = True
                if _cached_global_client is client or is_client_closed(_cached_global_client):
                    _cached_global_client = None
                    cleared = True
                if not cleared:
                    _cached_genai_client = None
                    _cached_global_client = None
                    cleared = True
        else:
            if _cached_genai_client is not None and is_client_closed(_cached_genai_client):
                _cached_genai_client = None
                cleared = True
            if _cached_global_client is not None and is_client_closed(_cached_global_client):
                _cached_global_client = None
                cleared = True
        return cleared


def _build_genai_client() -> genai.Client:
    if use_vertex():
        return genai.Client(
            vertexai=True,
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            location=os.environ["GOOGLE_CLOUD_LOCATION"],
        )
    return genai.Client(api_key=_api_key())


def genai_client() -> genai.Client:
    """Return a genai client for either Vertex AI or the Gemini API.

    Cached, and not only to save the handshake: a client built per call is
    dropped as soon as the call returns, and the underlying httpx pool is
    closed out from under any request still in flight.

    If the cached client is closed, it is refreshed.
    """
    global _cached_genai_client
    with _client_lock:
        if _cached_genai_client is None or is_client_closed(_cached_genai_client):
            _cached_genai_client = _build_genai_client()
        return _cached_genai_client


def _build_global_client() -> genai.Client:
    if not use_vertex():
        return genai_client()
    return genai.Client(
        vertexai=True,
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        location="global",
    )


def global_client() -> genai.Client:
    """Return a genai client on the global endpoint.

    3.8 Flash is served from `global` and nowhere else — measured, not
    assumed: `us-east1`, `us-central1` and `asia-northeast1` all answer a
    `generate_content` for it with a 404 naming the publisher model. So a tool
    on a 3.x model cannot go through :func:`genai_client`, which is pinned to
    ``GOOGLE_CLOUD_LOCATION`` and shared with Maps grounding, the weather
    geocoder and the cache summariser, all of which want the region.

    The sub-agents solve the same problem with ADK's ``client_kwargs``
    (``GWS_LOCATION`` in `gws.py`); this is that, for the tools that build
    their own request.

    No environment variable, unlike ``GWS_LOCATION``: a knob that moves this
    off `global` is a knob that makes the name a lie, and the fallback if
    `global` ever stops serving a model is to name a different model, not a
    different endpoint. 2.5 Flash is served here too, so the region is not
    what you would go back to.

    Cached for the same reason :func:`genai_client` is, and falls back to it
    off Vertex, where there is one endpoint and the argument means nothing.

    If the cached client is closed, it is refreshed.
    """
    if not use_vertex():
        return genai_client()
    global _cached_global_client
    with _client_lock:
        if _cached_global_client is None or is_client_closed(_cached_global_client):
            _cached_global_client = _build_global_client()
        return _cached_global_client


genai_client.cache_clear = clear_client_cache
global_client.cache_clear = clear_client_cache


def grounding_client() -> genai.Client:
    """Return a genai client for the grounding model.

    3.x models (such as the default `gemini-3.5-flash-lite`) are served on
    `location="global"` and 404 on regional endpoints like `us-east1`.
    Delegates to :func:`global_client`.
    """
    return global_client()

