"""Check what `_client.check_env()` says about each way of configuring this.

The function exists to turn a missing credential into one line at startup
instead of into a card that comes back thin, so the way it fails is by staying
quiet. That is invisible from inside the process, and every one of the answers
below was wrong at some point while looking right:

- **Vertex, everything set, no ADC.** The Pi's own configuration, and the
  failure the function was written for. It said nothing.
- **The Gemini-API path.** `search_url` and `authed_session` are used in both
  modes, so the project number and ADC are not Vertex's to require.
- **`GOOGLE_GENAI_USE_VERTEXAI=1`.** google-genai reads `true` or `1`; this
  module read `TRUE`, so the two disagreed about which path was configured.
- **`.env.example` copied and not edited.** Filling the template with
  `my-project-id` silenced the check for a fresh clone.
- **`GOOGLE_GENAI_USE_ENTERPRISE`.** Read first by the SDK and allowed to
  overrule `GOOGLE_GENAI_USE_VERTEXAI`; this module did not read it at all.
- **The Maps key.** Not a grounding credential, so `check_env` is right not to
  report it — and nothing else did either, which left the map on the place card
  failing to draw in silence. :func:`_client.check_browser_env` is its list.

Each case runs in a subprocess with an environment built from nothing, so the
real `.env` and the real ADC on this machine cannot reach it. ADC is pointed at
a temporary file: an authorized-user JSON where it should resolve, a path that
does not exist where it should not.

`GOOGLE_APPLICATION_CREDENTIALS` at a missing file is not the same refusal a
Pi gives, and reading it as one hid the cost in :func:`_client._have_adc` for a
round. `google.auth` short-circuits on an explicit path — no well-known file,
no metadata server, 0.0 s — where a machine that was never logged in spends
8.9 s on three retries against 169.254.169.254 first. Same
`DefaultCredentialsError`, two orders of magnitude apart, and the slow one is
the configuration that opened the issue. So the last block runs it: no
credentials variable at all, `HOME` and `CLOUDSDK_CONFIG` at an empty
directory. It is the only case here that touches the network, it fails to
reach anything, and it is worth the ten seconds it costs.

    uv run python tools/env_check.py

Runs every case and exits non-zero if any of them did not hold.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ai_terminal.live.tools._client import ADC  # noqa: E402

# What the subprocess does: import the module under a scrubbed environment and
# print the answer. Imported rather than reimplemented, so this checks the
# function and not a copy of its logic.
PROBE = (
    "import json, sys; sys.path.insert(0, %r);"
    " from ai_terminal.live.tools import _client;"
    " print(json.dumps(_client.check_env()))"
) % str(ROOT / "src")

BROWSER_PROBE = PROBE.replace("check_env()", "check_browser_env()")

# The tool report, on whichever machine `FAKE_SYSTEM` and `FAKE_MACHINE` name.
# Patched rather than read, because the answer turns on the architecture and
# there is no environment variable for one — and both machines that matter, the
# Pi and the developer's Mac, have to be answerable from either.
#
# Patched before the import and not after, so `NO_BUNDLED_CHROMIUM` is compared
# against the case's machine however `_client` reads it.
TOOL_PROBE = (
    "import json, os, platform, sys; sys.path.insert(0, %r)\n"
    "platform.system = lambda: os.environ['FAKE_SYSTEM']\n"
    "platform.machine = lambda: os.environ['FAKE_MACHINE']\n"
    "from ai_terminal.live.tools import _client\n"
    "print(json.dumps(_client.check_tool_env()))"
) % str(ROOT / "src")

# And the same machine seen from `main`, which is where the report is actually
# made. It prints nothing: what the case reads is what the import logged.
# `load_dotenv` is stubbed first, because `main.py:26` loads the real `.env`
# with `override=True` and would otherwise answer for whoever is running this
# rather than for the machine the case names.
STARTUP_PROBE = (
    "import os, platform, sys; sys.path.insert(0, %r)\n"
    "platform.system = lambda: os.environ['FAKE_SYSTEM']\n"
    "platform.machine = lambda: os.environ['FAKE_MACHINE']\n"
    "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
    "from ai_terminal.live import main\n"
) % str(ROOT / "src")

# Which endpoint each of the two clients ends up on, and whether they are the
# same object. A subprocess because both are `functools.cache`d, so the first
# environment a process sees is the only one it ever answers for.
#
# Nothing is sent. `genai.Client` resolves its endpoint at construction and
# stops there, so this reads the resolution without a network call or a
# credential.
CLIENT_PROBE = (
    "import json, sys; sys.path.insert(0, %r);"
    " from ai_terminal.live.tools import _client;"
    " g = _client.global_client(); r = _client.genai_client();"
    " print(json.dumps([g._api_client.location, r._api_client.location,"
    " g is r]))"
) % str(ROOT / "src")

# The same, three times over with a clock on it. Only the last block runs this,
# and only because the shape it runs under is the one where the clock says
# something.
TIMED_PROBE = (
    "import json, sys, time; sys.path.insert(0, %r)\n"
    "from ai_terminal.live.tools import _client\n"
    "runs = []\n"
    "for _ in range(3):\n"
    "    at = time.monotonic()\n"
    "    answer = _client.check_env()\n"
    "    runs.append([round(time.monotonic() - at, 3), answer])\n"
    "print(json.dumps(runs))"
) % str(ROOT / "src")

# A gcloud ADC file, which is an OAuth refresh token and nothing else.
# `google.auth` parses it and stops; it is not asked to mint anything here.
ADC_FILE = json.dumps({
    "type": "authorized_user",
    "client_id": "not-a-real-client.apps.googleusercontent.com",
    "client_secret": "not-a-real-secret",
    "refresh_token": "not-a-real-token",
})

failed = False
# Counted rather than written down, so the closing line cannot drift from the
# number of cases.
ran = 0


def check(name: str, got, want) -> None:
    global failed, ran
    ran += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        failed = True


def env_of(path: Path) -> dict[str, str]:
    """The `KEY=value` lines of a dotenv file, comments and blanks skipped.

    Enough of dotenv to read the template beside it, and it handles the three
    spellings the template does not use yet: a leading `export`, a quoted
    value, and a comment after the value. Those are what somebody writes the
    first time they edit it by hand, and each of them used to arrive here as
    part of the value — `KEY="a-project"` set the variable to `"a-project"`
    with the quotes in it, which every check below would have accepted.

    Not a dotenv parser. It does not do `${VAR}` interpolation, escapes, or a
    value that runs over more than one line, all of which python-dotenv — what
    actually loads the file at runtime — does. A template needing any of them
    is the template to stop hand-rolling this for and import the library.
    """
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip().removeprefix("export ").lstrip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        quote = value[:1]
        if quote in ("'", '"'):
            end = value.find(quote, 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = value.partition("#")[0].strip()
        out[key.strip()] = value
    return out


def probe(adc: bool | None = True, script: str = PROBE, **variables: str):
    """The probe script under exactly `variables`, and ADC set up three ways.

    `True` points `GOOGLE_APPLICATION_CREDENTIALS` at an authorized-user JSON,
    `False` points it at a path that is not there, and `None` does not set it
    at all — the last of which is the Pi, and the only one of the three that
    lets `google.auth` past the variable and out to the metadata server.

    `HOME` and `CLOUDSDK_CONFIG` at an empty directory in every case, so the
    ADC of whoever ran this is out of reach on the `None` path and cannot be
    reached by deleting one line on the other two.
    """
    env = {"PATH": os.environ.get("PATH", ""),
           "HOME": str(empty), "CLOUDSDK_CONFIG": str(empty), **variables}
    if adc is not None:
        env["GOOGLE_APPLICATION_CREDENTIALS"] = str(
            adc_present if adc else scratch / "never-logged-in.json")
    done = subprocess.run([sys.executable, "-c", script], env=env,
                          capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def missing(adc: bool | None = True, **variables: str) -> list[str]:
    """`check_env()` under exactly `variables`. See :func:`probe`."""
    return probe(adc, **variables)


def refusing(system: str, machine: str, **variables: str) -> list[str]:
    """`check_tool_env()` on a named machine. See :func:`probe`.

    `PATH` is emptied unless a case sets it. Whoever runs this may well have a
    `gws` installed — the author does — and a case whose answer depends on that
    is one that passes here and fails on the next machine.
    """
    return probe(script=TOOL_PROBE,
                 **{"PATH": str(empty), "FAKE_SYSTEM": system,
                    "FAKE_MACHINE": machine, **variables})


def startup_lines(system: str, machine: str, **variables: str) -> list[str]:
    """What `main` logs on its way up, on a named machine.

    Imported rather than read, because what this watches is a *call*.
    `check_tool_env` is pinned exhaustively above and would still be reporting
    to nobody if startup never asked it, which is the whole of the bug.

    Everything the import might write goes to the scratch directory, working
    directory included: `sessions.db` is the household's own conversations and
    `gws_cache` its Workspace documents, and both default to a relative path.
    """
    env = {"PATH": str(empty), "HOME": str(empty),
           "CLOUDSDK_CONFIG": str(empty),
           "GOOGLE_APPLICATION_CREDENTIALS": str(adc_present),
           "SESSIONS_DB": str(scratch / "sessions.db"),
           "GWS_CACHE_DIR": str(scratch / "cache"),
           "GOOGLE_MAPS_API_KEY": "a-key",
           "FAKE_SYSTEM": system, "FAKE_MACHINE": machine,
           **VERTEX, **variables}
    done = subprocess.run([sys.executable, "-c", STARTUP_PROBE], env=env,
                          cwd=scratch, capture_output=True, text=True,
                          check=True)
    # Level and message, because since #145 the level is part of the claim: the
    # shipped Pi has no `gws` on purpose and says so at `WARNING`, while a
    # `browse` that was meant to work and does not is still an `ERROR`. A list
    # of bare messages could not tell those two apart.
    said = []
    for line in done.stderr.splitlines():
        for level in ("ERROR", "WARNING"):
            if f" {level} ai_terminal.live.main: " in line:
                said.append(f"{level} {line.partition(': ')[2]}")
    return said


VERTEX = {"GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
          "GOOGLE_CLOUD_PROJECT": "a-project",
          "GOOGLE_CLOUD_LOCATION": "us-east1",
          "GOOGLE_CLOUD_PROJECT_NUMBER": "123456789012"}

with tempfile.TemporaryDirectory() as _scratch:
    scratch = Path(_scratch)
    adc_present = scratch / "adc.json"
    adc_present.write_text(ADC_FILE, encoding="utf-8")
    empty = scratch / "empty-home"
    empty.mkdir()

    print("== Vertex ==")
    check("everything set", missing(**VERTEX), [])
    # The one this function was written for and did not catch: the Pi had all
    # three variables and had never run `gcloud auth application-default
    # login`, so startup was silent and every grounded question came back
    # empty.
    check("everything set but no ADC", missing(adc=False, **VERTEX), [ADC])
    check("no project", missing(**{k: v for k, v in VERTEX.items()
                                   if k != "GOOGLE_CLOUD_PROJECT"}),
          ["GOOGLE_CLOUD_PROJECT"])
    check("no location", missing(**{k: v for k, v in VERTEX.items()
                                    if k != "GOOGLE_CLOUD_LOCATION"}),
          ["GOOGLE_CLOUD_LOCATION"])
    check("no project number", missing(**{k: v for k, v in VERTEX.items()
                                          if k != "GOOGLE_CLOUD_PROJECT_NUMBER"}),
          ["GOOGLE_CLOUD_PROJECT_NUMBER"])
    check("nothing at all", missing(adc=False,
                                    GOOGLE_GENAI_USE_VERTEXAI="TRUE"),
          ["GOOGLE_CLOUD_PROJECT_NUMBER", "GOOGLE_CLOUD_PROJECT",
           "GOOGLE_CLOUD_LOCATION", ADC])

    # `browse` runs on 3.8 Flash, which Vertex serves from `global` and nowhere
    # else — `us-east1`, `us-central1` and `asia-northeast1` all answer a
    # `generate_content` for it with a 404 naming the publisher model. So the
    # tool needs its own client, and the thing worth pinning is that the two do
    # not quietly become one: a `global_client` that read
    # `GOOGLE_CLOUD_LOCATION` like its neighbour would work on a machine
    # configured for `global` and 404 on every other one.
    print("\n== the global endpoint ==")
    endpoints = probe(script=CLIENT_PROBE, **VERTEX)
    check("browse goes to global", endpoints[0], "global")
    check("while everything else stays in the configured region",
          endpoints[1], "us-east1")
    check("so they are two clients", endpoints[2], False)
    # Off Vertex there is one endpoint and `location` means nothing, so a second
    # client would be a second httpx pool for no reason.
    off_vertex = probe(script=CLIENT_PROBE, GEMINI_API_KEY="a-key",
                       GOOGLE_CLOUD_PROJECT_NUMBER="123456789012")
    check("off Vertex there is nothing to be global about",
          off_vertex[0], None)
    check("and browse shares the one client", off_vertex[2], True)

    # The other path is not a smaller version of the Vertex one. Search
    # grounding is a project-number-scoped POST signed with ADC either way, so
    # a key on its own leaves `search_url()` raising `KeyError` on the first
    # question.
    print("\n== the Gemini API path ==")
    check("a key on its own is not enough",
          missing(GEMINI_API_KEY="a-key"), ["GOOGLE_CLOUD_PROJECT_NUMBER"])
    check("a key and a project number is",
          missing(GEMINI_API_KEY="a-key",
                  GOOGLE_CLOUD_PROJECT_NUMBER="123456789012"), [])
    check("and it needs ADC too",
          missing(adc=False, GEMINI_API_KEY="a-key",
                  GOOGLE_CLOUD_PROJECT_NUMBER="123456789012"), [ADC])
    check("no key of any of the three names",
          missing(GOOGLE_CLOUD_PROJECT_NUMBER="123456789012"),
          ["GOOGLE_CLOUD_API_KEY"])
    # google-genai falls back to these two, and the README tells you to set
    # GOOGLE_API_KEY for Chromium's geolocation — so a key can be in use that
    # `.env` never mentions, and reporting it missing would be a lie.
    for name in ("GOOGLE_CLOUD_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        check(f"{name} counts as the key",
              missing(GOOGLE_CLOUD_PROJECT_NUMBER="123456789012",
                      **{name: "a-key"}), [])

    # `_api_client.py:665` is `env_vertexai_str.lower() in ['true', '1']`.
    # Anything this module reads differently is a mode the SDK and the check
    # disagree about, and the check is the one that will be wrong.
    print("\n== which path is configured ==")
    for value in ("TRUE", "true", "True", "1"):
        check(f"{value!r} goes to Vertex",
              missing(GOOGLE_GENAI_USE_VERTEXAI=value,
                      GOOGLE_CLOUD_PROJECT_NUMBER="123456789012"),
              ["GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"])
    for value in ("", "FALSE", "0", "yes"):
        check(f"{value!r} does not",
              missing(GOOGLE_GENAI_USE_VERTEXAI=value,
                      GOOGLE_CLOUD_PROJECT_NUMBER="123456789012"),
              ["GOOGLE_CLOUD_API_KEY"])
    # `_api_client.py:656-681` reads GOOGLE_GENAI_USE_ENTERPRISE first and uses
    # its answer when the two conflict — it warns and carries on. Reading only
    # the second variable is the same disagreement as `=1` above, on a rarer
    # spelling: the SDK goes to Vertex and the check reports the Gemini-API
    # path, or the other way round.
    check("enterprise on its own goes to Vertex",
          missing(GOOGLE_GENAI_USE_ENTERPRISE="true",
                  GOOGLE_CLOUD_PROJECT_NUMBER="123456789012"),
          ["GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"])
    check("and beats a conflicting GOOGLE_GENAI_USE_VERTEXAI",
          missing(GOOGLE_GENAI_USE_ENTERPRISE="false",
                  GOOGLE_GENAI_USE_VERTEXAI="true",
                  GOOGLE_CLOUD_PROJECT_NUMBER="123456789012"),
          ["GOOGLE_CLOUD_API_KEY"])
    # Set and empty is set: the SDK branches on `is not None`, so this is
    # enterprise saying no rather than enterprise saying nothing.
    check("set and empty still beats it",
          missing(GOOGLE_GENAI_USE_ENTERPRISE="",
                  GOOGLE_GENAI_USE_VERTEXAI="true",
                  GOOGLE_CLOUD_PROJECT_NUMBER="123456789012"),
          ["GOOGLE_CLOUD_API_KEY"])
    check("but unset does not",
          missing(GOOGLE_GENAI_USE_VERTEXAI="true",
                  GOOGLE_CLOUD_PROJECT_NUMBER="123456789012"),
          ["GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"])

    # The page's own key, on its own list. `check_env` reporting it would put
    # it behind "grounding will fail", which it does not; leaving it unreported
    # is the silent failure this file exists to convert into a startup line —
    # Maps JS never loads and the place card draws a blank where the map goes.
    print("\n== the key the page needs ==")
    check("a Vertex setup with no Maps key still has no map",
          probe(script=BROWSER_PROBE, **VERTEX), ["GOOGLE_MAPS_API_KEY"])
    check("and with one, nothing to say",
          probe(script=BROWSER_PROBE, GOOGLE_MAPS_API_KEY="a-key", **VERTEX),
          [])
    # Two lists, and neither reads the other's. A key with nothing else set is
    # a page that works and a server that cannot ground anything.
    check("the key on its own answers the page's question",
          probe(adc=False, script=BROWSER_PROBE, GOOGLE_MAPS_API_KEY="a-key"),
          [])
    check("and none of the grounding one",
          missing(adc=False, GOOGLE_MAPS_API_KEY="a-key"),
          ["GOOGLE_CLOUD_PROJECT_NUMBER", "GOOGLE_CLOUD_API_KEY", ADC])

    # Neither of these is a variable being unset, so neither list above can
    # carry them, and both tools stay quiet until somebody asks — `browse` with
    # a Playwright message telling the user to run `playwright install`, which
    # on the Pi downloads nothing that will run, and the Workspace sub-agent
    # with `UNCONFIGURED` inside a tool result the model may simply retry.
    print("\n== the tools that answer for themselves ==")
    tool_bin = scratch / "bin"
    tool_bin.mkdir()
    for _name in ("gws", "chromium"):
        _stub = tool_bin / _name
        _stub.write_text("#!/bin/sh\n", encoding="utf-8")
        _stub.chmod(0o755)
    A_CHROMIUM = str(tool_bin / "chromium")
    HAVE_GWS = {"GWS_BIN": str(tool_bin / "gws")}
    NO_GWS = "gws (no gws on PATH; install it or set GWS_BIN)"
    NO_CHROMIUM = ("browse (no BROWSE_CHROMIUM, and Playwright ships no "
                   "arm64 Linux build)")
    NO_CHROMIUM_INSTALL = ("browse (no BROWSE_CHROMIUM, and Playwright Chromium is "
                           "not installed)")
    PI = ("Linux", "aarch64")

    pw_dir = scratch / "ms-playwright"
    pw_dir.mkdir()
    pw_chromium = pw_dir / "chromium-1234"
    pw_chromium.mkdir()
    (pw_chromium / "INSTALLATION_COMPLETE").write_text("", encoding="utf-8")
    HAVE_PLAYWRIGHT = {"PLAYWRIGHT_BROWSERS_PATH": str(pw_dir)}

    check("on an ordinary Linux box with no browser installed, browse refuses",
          refusing("Linux", "x86_64", **HAVE_GWS), [NO_CHROMIUM_INSTALL])
    check("on an ordinary Linux box, Playwright brings its own browser",
          refusing("Linux", "x86_64", **HAVE_GWS, **HAVE_PLAYWRIGHT), [])
    check("on the Pi's shape it does not, so browse refuses every page",
          refusing(*PI, **HAVE_GWS), [NO_CHROMIUM])
    check("on the Pi even with Playwright Chromium, arm64 Linux is still refused",
          refusing(*PI, **HAVE_GWS, **HAVE_PLAYWRIGHT), [NO_CHROMIUM])
    # Apple Silicon says `arm64` as well, and Playwright does publish a
    # darwin-arm64 build — this Mac is running one. Testing the architecture
    # alone reports a broken browser on the developer's own machine every
    # startup, which is the fastest way to teach everybody to skip the line.
    check("an arm64 Mac with no browser installed refuses",
          refusing("Darwin", "arm64", **HAVE_GWS), [NO_CHROMIUM_INSTALL])
    check("an arm64 Mac is not an arm64 Linux",
          refusing("Darwin", "arm64", **HAVE_GWS, **HAVE_PLAYWRIGHT), [])
    # The other way to get here, and the one where the message has to carry the
    # path: the variable is set, and whoever set it believes it.
    check("a BROWSE_CHROMIUM naming nothing is reported with the path",
          refusing("Linux", "x86_64", BROWSE_CHROMIUM="/opt/nope/chromium",
                    **HAVE_GWS),
          ["browse (BROWSE_CHROMIUM=/opt/nope/chromium is not an executable)"])
    # `Path.exists()` accepts a directory and Playwright cannot launch one; the
    # execute bit on a directory means searchable. `shutil.which` is this
    # repo's answer to exactly that, argued out at `_gws_cli.py:40-47`.
    check("nor is a directory something to launch",
          refusing("Linux", "x86_64", BROWSE_CHROMIUM=str(tool_bin),
                    **HAVE_GWS),
          [f"browse (BROWSE_CHROMIUM={tool_bin} is not an executable)"])
    # And a name is not a path, however well it resolves. `shutil.which` finds
    # a bare name on PATH; Playwright never looks there — `browser.py:616-618`
    # hands the value to `executable_path` and the driver's
    # `resolveExecutablePath` is an `exists` — so this boots silent and dies at
    # the first page. The stub on PATH is what makes it silent.
    check("a bare BROWSE_CHROMIUM is a name, and Playwright wants a path",
          refusing("Linux", "x86_64", BROWSE_CHROMIUM="chromium",
                    PATH=str(tool_bin), **HAVE_GWS),
          ["browse (BROWSE_CHROMIUM=chromium is a name, and Playwright needs "
            "a path to the executable)"])
    # Which is the point of saying anything: the Pi has a system Chromium, and
    # naming it is the fix the message asks for.
    check("and one that is there is nothing to say, on either shape",
          [refusing(*PI, BROWSE_CHROMIUM=A_CHROMIUM, **HAVE_GWS),
           refusing("Linux", "x86_64", BROWSE_CHROMIUM=A_CHROMIUM,
                    **HAVE_GWS)],
          [[], []])
    # An empty PATH is a machine with no `gws`. Nothing is run: the sub-agent
    # is never started and the binary is never invoked.
    check("no gws on PATH is the Workspace tool refusing every question",
          refusing("Linux", "x86_64", **HAVE_PLAYWRIGHT), [NO_GWS])
    check("a GWS_BIN pointing at one is nothing to say",
          refusing("Linux", "x86_64", **HAVE_GWS, **HAVE_PLAYWRIGHT), [])
    # Its own message, because telling somebody to set a variable they have set
    # reads as though it were not being read — and `shutil.which` never
    # consults PATH for a value with a separator in it, so "not on PATH" would
    # be false into the bargain.
    check("a GWS_BIN pointing nowhere names the variable, not PATH",
          refusing("Linux", "x86_64", GWS_BIN="/opt/nope/gws", **HAVE_PLAYWRIGHT),
          ["gws (GWS_BIN=/opt/nope/gws is not an executable)"])
    check("the Pi's own configuration, both lines at once",
          refusing(*PI), [NO_CHROMIUM, NO_GWS])
    check("an unconfigured Linux machine, both lines at once",
          refusing("Linux", "x86_64"), [NO_CHROMIUM_INSTALL, NO_GWS])

    # And whether anything asks. Every case above still holds with the
    # `main.py` block deleted, which is the bug rather than a variant of it:
    # the tools already knew, and startup was not asking them.
    check("the server says it on its way up, before anybody asks",
          startup_lines(*PI),
          [f"WARNING these tools will refuse work: {NO_GWS}",
           f"ERROR these tools will refuse work: {NO_CHROMIUM}"])
    # The reading the shipped Pi is meant to have, and the whole reason the two
    # are split: no `gws` is a decision, and a red line at every boot for the
    # life of a correctly installed machine is how a level stops meaning
    # anything. A `browse` entry beside it is a fault and stays red.
    check("and a Pi missing only the tool it was built without is not an error",
          startup_lines(*PI, BROWSE_CHROMIUM=A_CHROMIUM),
          [f"WARNING these tools will refuse work: {NO_GWS}"])
    check("and a machine missing Playwright Chromium logs ERROR at startup",
          startup_lines("Darwin", "arm64", **HAVE_GWS),
          [f"ERROR these tools will refuse work: {NO_CHROMIUM_INSTALL}"])
    check("while a machine with both keeps quiet",
          startup_lines("Darwin", "arm64", **HAVE_GWS, **HAVE_PLAYWRIGHT), [])
    # And the probe answers for the machine it names rather than for the one
    # running it. Through the Maps key, because that is the variable a real
    # `.env` here is most likely to hold: emptied by the case and reported
    # back, unless `main.py:29` has loaded the developer's file over the top.
    # On a clone with no `.env` there is nothing to leak and it holds anyway.
    check("and the real .env cannot reach the report",
          startup_lines("Darwin", "arm64", GOOGLE_MAPS_API_KEY="",
                        **HAVE_GWS, **HAVE_PLAYWRIGHT),
          ["ERROR place cards will draw without a map, "
           "missing: GOOGLE_MAPS_API_KEY"])

    # The template is the first `.env` anybody has, and filling it with
    # plausible values makes the check pass for a clone that cannot work.
    print("\n== .env.example, copied and not edited ==")
    check("the two that have to be edited are reported",
          missing(**env_of(ROOT / ".env.example")),
          ["GOOGLE_CLOUD_PROJECT_NUMBER", "GOOGLE_CLOUD_PROJECT"])
    # `env_of` reading a value with its quotes still on would set
    # `GOOGLE_CLOUD_PROJECT` to `"my-project"` — a different string, and every
    # case above would accept it. Nothing in the template is written this way
    # yet, so the case has to build the file.
    written_by_hand = scratch / "by-hand.env"
    written_by_hand.write_text(
        '# a comment\n'
        'export GOOGLE_CLOUD_PROJECT_NUMBER="123456789012"\n'
        "  GOOGLE_CLOUD_PROJECT = 'a-project'  # the one to edit\n"
        "GOOGLE_CLOUD_LOCATION=us-east1   # unquoted, trailing comment\n"
        "GOOGLE_GENAI_USE_VERTEXAI=TRUE\n"
        "\n",
        encoding="utf-8")
    check("export, quotes and trailing comments are not part of the value",
          env_of(written_by_hand),
          {"GOOGLE_CLOUD_PROJECT_NUMBER": "123456789012",
           "GOOGLE_CLOUD_PROJECT": "a-project",
           "GOOGLE_CLOUD_LOCATION": "us-east1",
           "GOOGLE_GENAI_USE_VERTEXAI": "TRUE"})
    check("so a hand-written file reads as complete",
          missing(**env_of(written_by_hand)), [])

    # The configuration this issue is about, in the shape it actually has: no
    # `GOOGLE_APPLICATION_CREDENTIALS`, nothing in the well-known place, and no
    # metadata server to answer. Every case above sets the variable, which
    # short-circuits `google.auth` before any of that — so this is the only one
    # here that pays what a Pi pays, and the only one that can see the answer
    # is cached.
    print("\n== a machine that was never logged in ==")
    runs = probe(None, TIMED_PROBE, **VERTEX)
    print(f"   {[took for took, _ in runs]} seconds per check_env()")
    check("ADC is reported missing, and nothing else is",
          [answer for _, answer in runs], [[ADC]] * 3)
    # `functools.cache` stores return values and a raise is not one, so this
    # used to be the full lookup every time — three metadata retries per
    # grounding branch per question, on the machine least able to spare them.
    check("and the refusal is resolved once, not once per caller",
          [took < runs[0][0] / 10 for took, _ in runs], [False, True, True])

print(f"\nall {ran} cases hold" if not failed else f"\nsome of {ran} cases failed")
sys.exit(1 if failed else 0)
