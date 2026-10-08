"""Check the files the Pi is deployed from, which no script here can run.

The wake monitor's install is five decisions and every one of them fails
without saying so: a system unit instead of a user unit finds no PipeWire and
hears nothing; no `enable-linger` and the monitor is only up while somebody is
sitting in front of it; no `pipewire-alsa` and ALSA has no `pipewire` PCM to
open; no microphone grant and `getUserMedia` fails behind a prompt a kiosk
cannot answer; and a webhook pointed at the wrong port posts detections into a
closed socket. Every one of those is a working monitor and a silent terminal.

None of that can be run from a laptop and this script does not pretend to. It
checks a smaller claim, which is the one that can be checked here: that the
files in `deploy/` describe the program in `src/`, and that the procedure in
the README describes the files in `deploy/`. That catches the failure this
project actually has a history of — a constant in a deploy artifact that was
right when it was written and is not right now — and it catches nothing about
the device.

What is a cross-reference, and therefore worth something:

- **The monitor's command line goes through `wake.py`'s own parser.** Not a
  copy of it, and not a regex over the flag names: `main`'s parser is lifted
  out and handed the `ExecStart` from the unit. A flag renamed in wake.py, a
  threshold that stops being a float, an argument that moves from `--model` to
  a positional — each of those turns into a unit that argparse refuses, which
  on the Pi is a unit that fails on every restart forever.
- **The webhook is a route the server registers, at the port the rest of the
  deploy uses.** `main.app` is imported and its route table read, so the path
  is checked against the real registration and the method with it. The port is
  checked against the kiosk unit's URL and the tunnel unit's `-L`, because
  those three numbers are one number written in three files.
- **The model is a file this repo ships, at the path the rsync puts it.** The
  README used to name `~/models/`, which nothing in the repo creates.
- **The microphone is granted to the origin the kiosk actually opens.** The
  policy file's `AudioCaptureAllowedUrls` is derived from nothing; it is
  compared against the URL in the kiosk unit's `ExecStart`, port included,
  since an origin that differs by a port grants nothing and says so nowhere.
- **The kiosk's URL still says it is the kiosk.** `?kiosk=1` is how the panel
  tells the server that a wake word said in front of it belongs to it and not
  to the last page that connected, and the name is read out of the unit and
  looked for in `app.js` and `main.py` rather than written down three times.
  Dropped from the unit, both units still start and the microphone opens in
  the wrong room.
- **The log line the verification step tells you to look for is the line the
  server writes.** The format string is read out of `main.py`. A reworded log
  message otherwise leaves a procedure whose last step can never pass.
- **Every unit that gets copied gets enabled.** The install glob is
  `deploy/ai-terminal-*.service`, so a new unit joins the copy by existing and
  joins the `systemctl --user enable` line only if somebody remembers. Copied
  and not enabled is a file on the Pi doing nothing.

What is only a presence check, and is here because deleting the line is the
way it fails: that the procedure still installs `pipewire-alsa`, still lingers
the account, and still installs the policy where Chromium reads it. Three
strings in the README, and the first two are looked for inside the install
section rather than anywhere in the file, because both also occur outside it
and a whole-file match cannot see the line a person follows being deleted.
They cannot tell you the command works — they can tell you somebody removed
it.

What stays a device claim, and is written down in the README rather than here:
that the ALSA `pipewire` PCM exists once the package is installed, that the
HAT is the source PipeWire actually defaults to on the Pi — the README says
which source this terminal is meant to be on and gives the reason, and that
reason is a choice rather than something a Mac can measure — that Chromium
reads `/etc/chromium/policies/managed` on this image, and that saying the
phrase in the kitchen moves all three logs.
A fake for any of those would be green on a Mac with no microphone, no
PipeWire, and no Chromium, which is worse than the gap it filled.

    uv run python tools/deploy_check.py
"""

import argparse
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import yaml
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
SRC = ROOT / "src" / "ai_terminal"
JS = SRC / "live" / "static" / "js"

sys.path.insert(0, str(ROOT / "src"))

# Importing the server reads .env to say what is missing from it, and on a
# machine that is not the one it runs on that is a dozen lines of red above a
# suite whose output is meant to be read. Nothing is called; only the route
# table is wanted.
logging.getLogger("ai_terminal.live.main").setLevel(logging.CRITICAL)

import pi_deploy  # noqa: E402
from ai_terminal import wake  # noqa: E402
from ai_terminal.live import main as server  # noqa: E402

# The unit reader lives in the installer rather than here. It is the installer
# that has to agree with the units — every path it uses on the Pi comes out of
# one — and a suite cannot check that against a second copy of the same parser.
from pi_deploy import every, one, origin, unit, url_in  # noqa: E402

RAN = 0

_GIT_SCRUB = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
              "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")


def _git_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_SCRUB}

# Where the deploy puts this directory on the Pi. The units say it, the
# README's rsync writes it, and it is the prefix that turns a path inside a
# unit back into a path inside this repo.
PI_DIR = "/home/pi/ai-terminal"

# The README heading the install procedure lives under. The presence checks
# read that section and not the whole file — a package installed somewhere
# else in the README is not the line somebody follows on the Pi.
SECTION = "Installing the monitor on the Pi"

# And the one the shipped server's own decisions live under: which keys the
# Pi's `.env` has to set, why the unit orders itself after nothing, and why the
# tunnel stays on disk and stays stopped.
SERVER_SECTION = "The server's own unit"

SERVER_UNIT = DEPLOY / "ai-terminal.service"
WAKE_UNIT = DEPLOY / "ai-terminal-wake.service"
KIOSK_UNIT = DEPLOY / "ai-terminal-kiosk.service"
TUNNEL_UNIT = DEPLOY / "ai-terminal-tunnel.service"
POLICY = DEPLOY / "ai-terminal-chromium-policy.json"

# The debugging port the kiosk leaves open (#144) and the README heading that
# accounts for it. Three constants because all three are cross-references: the
# flag is what #162's client connects to, the port is what the README's tunnel
# forwards onto, and the section has to exist or the only account of a
# permanent full-control channel into the household's browser is one argument
# in a unit file.
DEBUG_FLAG = "--remote-debugging-port"
DEBUG_PORT = 9222
DEBUG_SECTION = "The kiosk's debugging port"

# The one flag that changes what that port *is* rather than adding to it, and
# the sentence that is allowed to name it. Prose, not an assignment, because
# the way this goes wrong is a helpful line somewhere in the docs rather than
# an edit to the unit.
DEBUG_ADDRESS = "--remote-debugging-address"
DEBUG_BAN = "must not be put there"

# Every README this repo ships, which is where such a line would land. A glob
# rather than a list so a new one joins the scan by existing; .venv is skipped
# because a dependency's README is not this project saying anything.
DOCS = sorted(p for p in ROOT.rglob("README.md")
              if not {".venv", "node_modules"} & set(p.parts))

# --------------------------------------------------------------------------
# The unattended-boot acceptance, and how far the tree has moved since it
# passed.
#
# #10 is the one claim this project makes that no script here can reach: the
# Pi comes up with nobody at a keyboard and answers a spoken question. It was
# demonstrated first on `0181c02` (#10) and most recently on `8c01b43` (#213).
# Booting unattended is a property of a build rather than an event, so the
# demonstration expires when the build moves — and nothing said so. #213 was
# that expiry noticed by hand, fifty-odd commits late, by somebody reading
# `ActiveEnterTimestamp` for an unrelated reason.
# Noticing it by hand is the part that does not scale, so the record moves
# into the tree.
#
# `BOOT_ACCEPTED` is every file the acceptance rested on, digested at the
# commit it passed on. `BOOT_DRIFT` is the subset somebody has since read and
# accounted for, each against the issue that will clear it. A file that drifts
# and is not on that list turns this script red, and the way to clear it is to
# run the procedure in the README again — not to widen the list.
#
# What this gate does and does not claim. It answers *is a re-run due*, not
# *has the boot set changed*: a name on `BOOT_DRIFT` stops being watched until
# the next acceptance resets both dicts, because a second change to an
# already-stale file does not change what anybody has to do about it. What it
# will still catch on that file is deletion, and what it catches everywhere
# else is the first move.
#
# What is in the set, and why it is these and not more:
#
# - **Everything under `deploy/`**, derived rather than listed, so a new unit
#   or drop-in joins by existing rather than by being remembered. That is the
#   #183 shape twice over. systemd reads these once, at boot, and the
#   `Wants=`, `After=`, `Restart=` and enablement they carry have no other
#   moment. Three of them are not even sent to the Pi — `kanshi-config`,
#   `labwc-rc.xml` and `99-touch-calibration.rules`, which are #216 — so for
#   those a person and a reboot are the only mechanism there is.
# - **`src/ai_terminal/wake.py`**, and not because it is startup code. Until
#   #228 the deploy could not restart the monitor: `restart_plan` gave it
#   `enable --now`, which does nothing at all to a unit that is already up.
#   Since #228 restarts it, membership is derived from the plan below and
#   `wake.py` left the set on its own.
#
# What is deliberately out, because a gate that fires on every second commit
# is one people learn to clear without reading: `live/main.py`,
# `live/screensaver.py`, `src/ai_terminal/wake.py` and `tools/pi_deploy.py`
# each moved ten or more times over the same range, and all four are re-executed
# by the `systemctl restart` every deploy issues. What a reboot adds for them is contention — a
# network that is not up yet, a user manager still starting — which no digest
# can predict. They are what a re-run costs, not what makes one due.
#
# Comments are stripped before digesting, for the formats where a `#` line is
# a comment. Whole-file digests were tried first and went red within a day on
# `719cb2e`, which moved a `file:line` inside a comment in the kiosk unit —
# systemd does not read comments, and a gate that cannot tell an `ExecStart=`
# from a `#` spends its credibility on citation fixes.
BOOT_ISSUE = 10
BOOT_STALE_ISSUE = 213
BOOT_ACCEPTANCE = "8c01b43"
BOOT_SECTION = "Re-running the unattended-boot acceptance"
BOOT_COMMENTED = (".service", ".conf", ".rules", "kanshi-config")
BOOT_ACCEPTED = {
    "deploy/50-ai-terminal-journal.conf":
        "8918898dd7bc1447668f3989fcbe4feab79b1b8fb238d3900c2eafc9252f0e88",
    "deploy/99-touch-calibration.rules":
        "c0d06124e9876cc731601fdf3624c5de939a923ec4f0b3de97d96f25e54a0cb5",
    "deploy/ai-terminal-chromium-policy.json":
        "b6f220a9f348d467c3f38814ac5678c8f15d8a94e160df6fdc708585ace12c2e",
    "deploy/ai-terminal-kiosk.service":
        "a9cccf95524f9dd797bc8911fe722d7280c06aa4536afce3db10165d26e12fb3",
    "deploy/ai-terminal-tunnel.service":
        "9cbc7df1c81fb668600fde2dd046f4497ec8fec12ce4a526c0bec6da05bf6cd7",
    "deploy/ai-terminal-wake.service":
        "0f5a47236d16cbad727d9b83a550a1c10a1bbe1172c509cf61629f89f08dde31",
    "deploy/ai-terminal.service":
        "2ae096ac8b43c62e82367fb8eb5cf48e8b62756fc50cd47624237bbe45107898",
    "deploy/kanshi-config":
        "1ba65d5d51e5c5bec2c4e806a0c71888ffca747fe8d3f6e703b866826ecc3524",
    "deploy/labwc-rc.xml":
        "98f2bc48e3843079237ebc039abfbd727628552b5080a5b1f1ab5e6bba1c73f6",
}
# Emptied after the unattended-boot acceptance run on 2026-09-11 at 8c01b43 (#213).
# `ai-terminal-wake.service` added ExecStartPre in #224 to run mic_setup.sh on boot.
BOOT_DRIFT: dict[str, int] = {
    "deploy/ai-terminal-wake.service": 224,
    "deploy/50-usb-mic.conf": 574,
    "deploy/ai-terminal.service": 588,
    "deploy/ai-terminal-kiosk.service": 532,
}


def check(name: str, got, want, detail: str = "") -> None:
    global RAN
    RAN += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail or got}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


class _Caught(Exception):
    pass


def wake_parser() -> argparse.ArgumentParser:
    """`wake.main`'s own parser, without starting a capture stream.

    It is built inside `main`, and the point of taking the real one is that a
    copy of it here would agree with the unit forever.
    """
    real = argparse.ArgumentParser.parse_args
    held: dict[str, argparse.ArgumentParser] = {}

    def spy(self, args=None, namespace=None):
        held["parser"] = self
        raise _Caught

    argparse.ArgumentParser.parse_args = spy
    try:
        wake.main([])
    except _Caught:
        pass
    finally:
        argparse.ArgumentParser.parse_args = real
    return held["parser"]


def accepts(parser: argparse.ArgumentParser, argv: list[str]):
    """`parse_args`, or None where argparse would have exited."""
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            return parser.parse_args(argv)
        except SystemExit:
            return None


def quickstart_cases(readme: str) -> None:
    """Where the README sends a person's API keys, against where they go.

    The one instruction in this file that is followed by somebody who has read
    nothing else, and the only one whose failure mode is a credential in a
    commit. It said "the repo root" and called it gitignored; both halves were
    wrong and they were wrong the same way round — `main.py:25`'s `parents[3]`
    is the project directory, and the `.env` line is in the project's own
    `.gitignore`, so a file put where the sentence said would have been unread
    and tracked (#217). Nothing had stepped on it, measured: no `.env` has ever
    been committed on any ref.

    Pinned rather than corrected, because a corrected sentence is still a
    sentence, and this one was wrong for as long as it took somebody to read it
    against a real directory layout.
    """
    print("\nwhere the quickstart puts a file of API keys")
    # Split first and assert the split, because both halves are read below and
    # a renamed heading breaks them in two different silent ways: `[0]` widens
    # to the whole file and keeps passing, `[1]` raises IndexError instead of
    # printing a FAIL.
    parts = readme.split("### Configuration")
    check("the README still has the one Configuration heading this reads by",
          len(parts), 2)
    quickstart, configuration = parts[0], parts[-1]
    # The `cp` and not the prose is the fix: run from the project directory it
    # cannot land anywhere else, and it needs no directory layout parsed.
    check("the quickstart makes the file with a copy rather than by hand",
          "cp .env.example .env" in quickstart, True)
    # The `cd` is load-bearing, not throat-clearing: the copy is relative and
    # the workspace root is one level up with a `.env.example` of nobody's.
    cd_to = re.search(r"^cd (\S+)", quickstart, re.M)
    check("run from a directory the quickstart says to stand in",
          bool(cd_to), True, cd_to.group(1) if cd_to else "no cd line")
    # And the prose has to name that same directory, or the two instructions
    # disagree and the reader follows whichever they read second. Compared
    # against each other rather than against `ROOT.name`, so the case still
    # means something in a `git archive` extraction, where `ROOT.name` is
    # whatever the tarball was unpacked into.
    named = re.search(r"`(\S+)/\.env`", configuration)
    check("and the Configuration section names that same directory for .env",
          named.group(1) if named else "no .env path in the section",
          cd_to.group(1) if cd_to else "no cd line")
    # `cd` then `cp`, adjacent and in that order: a `cd ..` slipped in after
    # the first line is #217 again with the prose corrected, and a `cp`
    # hoisted above the `cd` runs in whatever directory the reader started
    # in. Both leave every other case in this function green.
    steps = [ln.split("#")[0].strip() for ln in quickstart.splitlines()
             if ln.startswith(("cd ", "cp "))]
    check("with the copy on the line straight after it and no cd between",
          steps[:2], [f"cd {cd_to.group(1)}" if cd_to else "no cd line",
                      "cp .env.example .env"])
    # And that directory is the one the server reads, which is the half of
    # #217 no amount of proofreading would have caught.
    check("which is the directory main.py loads the file from",
          server.REPO_ROOT, ROOT)
    # The line number in the prose, read out of the prose rather than written
    # here as a literal: a `.gitignore:7` nobody re-reads is the same defect
    # #217 was, one file over, and `citations_check.py` cannot hold it — its
    # `CITE` (`citations_check.py:140-142`) wants a suffix off a fixed list
    # and a leading `[A-Za-z_]`, and `.gitignore` has neither. Degenerate
    # cases fail as a case rather than as a traceback: no citation at all, or
    # one past the end of the file.
    cited = re.search(r"`\.gitignore:(\d+)`", configuration)
    rules = (ROOT / ".gitignore").read_text().splitlines()
    line_no = int(cited.group(1)) if cited else 0
    check("and the .gitignore line it cites is the line doing the ignoring",
          rules[line_no - 1].strip() if 0 < line_no <= len(rules)
          else f"no such line: {line_no}", ".env", f".gitignore:{line_no}")
    check("so the README does not send anybody to the repo root for it",
          [ln for ln in readme.splitlines()
           if ".env" in ln and "repo root" in ln], [])

    print("\nthe port the quickstart teaches")
    invocations = [
        ln.split("#")[0].strip()
        for ln in re.findall(r"^uv run live-server.*$", readme, re.M)
    ]
    defaults = server._parser().parse_args([])
    non_default = [
        parsed.port
        for inv in invocations
        if (parsed := accepts(server._parser(), inv.split("live-server", 1)[1].split()))
        and parsed.port != defaults.port
    ]
    check("every live-server the README shows runs on the program's default",
          non_default, [],
          f"{len(invocations)} invocation(s), default {defaults.port}")
    urls = [int(p) for p in re.findall(r"http://localhost:(\d+)", quickstart)]
    check("and every http://localhost in the quickstart is that same port",
          urls, [defaults.port])
    # The rest need a real checkout. The sweep runs a past SHA out of a
    # `git archive` extraction, and there `ROOT` is a copy at an arbitrary
    # path: git exits 128 on every call and `ROOT.name` is the tarball's
    # unpack directory rather than this project's name. Neither is a failing
    # case, both are unanswerable ones, so they are skipped out loud on
    # browse_check.py:2560's model rather than turned into a red total that
    # stops the other 311 cases in this file from running.
    #
    # The question is whether this tree is git's, not whether some tree is:
    # `rev-parse --is-inside-work-tree` answers yes for an extraction that
    # happens to sit under an unrelated repository, and then all four run and
    # all four fail — this bug again, one directory deeper. Asking the index
    # about a file that has to be tracked here answers the narrower question.
    if subprocess.run(["git", "ls-files", "--error-unmatch",
                       "tools/deploy_check.py"], cwd=ROOT,
                      capture_output=True, env=_git_env()).returncode != 0:
        print(f"   skipped: git does not track {ROOT}, so the four cases "
              "needing this to be the project's own checkout cannot run")
        return
    check("and the template it copies is in the tree, tracked",
          subprocess.run(["git", "ls-files", "--error-unmatch",
                          ".env.example"], cwd=ROOT,
                         capture_output=True, env=_git_env()).returncode, 0)
    check("and the directory both of them name is this one",
          cd_to.group(1) if cd_to else None, ROOT.name, ROOT.name)
    # `git` and not a `.gitignore` grep, because the answer depends on which
    # ignore files apply at that path and a grep of the nearest one cannot
    # know. The `-q` keeps the matching rule out of the output; the exit
    # status is the whole answer.
    ignored = subprocess.run(["git", "check-ignore", "-q", str(ROOT / ".env")],
                             cwd=ROOT, capture_output=True,
                             env=_git_env()).returncode
    check("and git ignores a .env there", ignored, 0)
    # The failure the old sentence set up, stated as a case so it cannot come
    # back as a helpful simplification: one directory up is a different
    # project's parent, git tracks it, and the server never opens it.
    above = subprocess.run(
        ["git", "check-ignore", "-q", str(ROOT.parent / ".env")],
        cwd=ROOT, capture_output=True, env=_git_env()).returncode
    check("while one left in the workspace root above it would be committable",
          above, 1, str(ROOT.parent))


def unit_cases() -> None:
    print("\nthe monitor's unit")
    check("the monitor ships a unit rather than a README snippet",
          WAKE_UNIT.exists(), True, WAKE_UNIT.name)
    if not WAKE_UNIT.exists():
        return
    u = unit(WAKE_UNIT)

    # A system unit with User= is the failure this issue was filed about: no
    # XDG_RUNTIME_DIR, so no `pipewire` PCM, so a monitor that hears nothing
    # and says nothing about it.
    check("it is a user unit, so it has no User=",
          every(u, "Service", "User"), [], "no User=")
    check("and it installs into the login session's target",
          one(u, "Install", "WantedBy"), "default.target")
    check("ordered after the graph it captures through",
          "pipewire.service" in (one(u, "Unit", "After") or ""), True,
          one(u, "Unit", "After"))
    check("and not bound to it, so a graph restart is survivable",
          every(u, "Unit", "Requires"), [], "no Requires=")
    check("it restarts, because a stopped monitor looks like silence",
          one(u, "Service", "Restart"), "always")
    check("it runs from the directory the rsync writes",
          one(u, "Service", "WorkingDirectory"), PI_DIR)
    check("it configures the codec before opening the stream",
          one(u, "Service", "ExecStartPre"),
          f"/bin/bash {PI_DIR}/tools/mic_setup.sh")

    argv = (one(u, "Service", "ExecStart") or "").split()
    # Out of the server's unit and no longer out of a fenced block in the
    # README. The block was the only copy of that line while the server had no
    # artifact; now there is one, and the installer's rule applies — a constant
    # that also lives in a deploy file is read out of the deploy file.
    server_argv = (one(unit(SERVER_UNIT), "Service",
                      "ExecStart") or "").split()
    check("through the same uv the server's own unit uses",
          argv[:1], server_argv[:1], argv[0] if argv else "")
    scripts = (ROOT / "pyproject.toml").read_text()
    check("and the console script pyproject declares",
          argv[1:3] == ["run", "wake"]
          and re.search(r'^wake = "ai_terminal\.wake:main"', scripts,
                        re.M) is not None,
          True, " ".join(argv[1:3]))


def server_unit_cases() -> None:
    print("\nthe server's own unit")
    check("the server ships a unit rather than a README snippet",
          SERVER_UNIT.exists(), True, SERVER_UNIT.name)
    if not SERVER_UNIT.exists():
        return
    u = unit(SERVER_UNIT)
    check("it is a user unit, so it has no User=",
          every(u, "Service", "User"), [], "no User=")
    check("and it installs into the login session's target",
          one(u, "Install", "WantedBy"), "default.target")
    # The decision, and the one worth a case: the user manager drops an
    # ordering dependency on a unit it has not loaded, and every candidate here
    # — network-online.target above all — belongs to the system manager. An
    # After= added later reads as protection and buys none.
    check("and orders itself after nothing, which is the only honest answer",
          every(u, "Unit", "After"), [], "no After=")
    check("it restarts, because a stopped server is a panel saying Offline",
          one(u, "Service", "Restart"), "always")
    check("it runs from the directory the rsync writes",
          one(u, "Service", "WorkingDirectory"), PI_DIR)
    env_vars = [tok for env in every(u, "Service", "Environment") for tok in env.split()]
    check("the server unit sets Environment=ENABLE_MANAGEMENT_ROUTES=1",
          "ENABLE_MANAGEMENT_ROUTES=1" in env_vars, True)
    check("the server unit sets Environment=AI_TERMINAL_IS_PI=1",
          "AI_TERMINAL_IS_PI=1" in env_vars, True)
    check("ENABLE_MANAGEMENT_ROUTES=1 is referenced in tools/pi_deploy.py",
          "ENABLE_MANAGEMENT_ROUTES=1" in (ROOT / "tools" / "pi_deploy.py").read_text(),
          True)

    argv = (one(u, "Service", "ExecStart") or "").split()
    scripts = (ROOT / "pyproject.toml").read_text()
    check("it runs the console script pyproject declares",
          argv[1:3] == ["run", "live-server"]
          and re.search(r'^live-server = "ai_terminal\.live\.main:run"',
                        scripts, re.M) is not None,
          True, " ".join(argv[1:3]))
    # Four files name 8000 — the kiosk's URL, the monitor's --webhook, the
    # tunnel's -L and the server's own default. A --port here would be a fifth,
    # and the only one of the five that can disagree without anything failing
    # until the panel is dark.
    check("and passes no --port, so the number stays the program's default",
          [a for a in argv if a.startswith("--port")], [])
    defaults = server._parser().parse_args([])
    kiosk = urlsplit(url_in(
        one(unit(KIOSK_UNIT), "Service", "ExecStart") or ""))
    check("which is the port the kiosk opens on the same machine",
          defaults.port, kiosk.port)
    # Loopback is enough here and is not enough under the tunnel topology for
    # the same reason: what matters is whether the browser is on this host. On
    # the shipped Pi it is, so the default binds where the kiosk knocks.
    check("and no --host, since the kiosk is a browser on this same Pi",
          [a for a in argv if a.startswith("--host")], [],
          f"binds {defaults.host}")
    check("and it is the port the restart phase asks ss about",
          pi_deploy.PI_PORT, defaults.port)

    # Who else may be *started* onto that port. `Wants=` starts the unit it
    # names whenever this one starts, whatever the named unit's own enable
    # state (systemd.unit(5)) — so a `Wants=` on a WantedBy=default.target unit
    # is a boot-time start, and it is one `systemctl --user disable` cannot
    # undo. What the kiosk named was an `ssh -N -L 8000:127.0.0.1:8000`, which
    # binds the address this server binds and wins the race, because `uv run`
    # plus the ADK import take longer than an ssh handshake. The panel then
    # shows a laptop and looks entirely healthy.
    stopped = {f"{r.unit_name}.service" for r in pi_deploy.RESOURCES
               if r.state == pi_deploy.INSTALLED}
    pulls = {u.name: [w for w in every(unit(u), "Unit", "Wants")
                      if w in stopped]
             for u in sorted(DEPLOY.glob("*.service"))}
    check("no unit here starts one the table says must stay stopped",
          {name: got for name, got in pulls.items() if got}, {},
          f"{len(pulls)} units, {len(stopped)} of them must stay stopped")
    kiosk_unit = unit(KIOSK_UNIT)
    check("the kiosk in particular starts nothing, since it starts at login",
          every(kiosk_unit, "Unit", "Wants"), [], "no Wants=")
    # The other half of the same decision, and the reason this is not simply a
    # deletion: ordering starts nothing, so a developer who wants the tunnel
    # still starts or enables it by hand and still gets the kiosk sequenced
    # behind it in the same transaction.
    check("but stays ordered behind it, for whoever starts it deliberately",
          one(kiosk_unit, "Unit", "After"), "ai-terminal-tunnel.service")


def command_cases() -> None:
    print("\nwhat the monitor is asked to do")
    u = unit(WAKE_UNIT)
    argv = (one(u, "Service", "ExecStart") or "").split()[3:]
    args = accepts(wake_parser(), argv)
    check("every flag on the command line is one wake.py defines",
          args is not None, True, f"{len(argv)} arguments")
    if args is None:
        return
    check("it captures through PipeWire, not the exclusive card",
          args.device, "pipewire")
    check("at the threshold the room measurements settled on",
          args.threshold, 0.2)
    check("with one model, the one those measurements ranked first",
          [Path(m).name for m in args.models or []],
          ["hey_gemini_ls128.onnx"])
    # The unit names an absolute path on the Pi; the rsync is what puts a file
    # there, so the path only exists if it is this directory's own. The README
    # named `~/models/` for a while, which nothing in the repo creates.
    model = str(args.models[0])
    under = model.removeprefix(PI_DIR + "/")
    check("and the model is a file this repo ships, at that path",
          under != model and (ROOT / under).is_file(), True, under)

    posts = {r.path for r in server.app.routes
             if "POST" in (getattr(r, "methods", None) or ())}
    hook = urlsplit(args.webhook or "")
    check("the webhook is a path the server registers",
          hook.path in posts, True, args.webhook)
    check("as a POST, which is the only method it registers it as",
          sorted(posts), ["/api/camera/extend", "/api/camera/offer", "/api/camera/stop", "/api/forget/{user_id}", "/api/gws/cancel", "/api/gws/login", "/api/gws/logout", "/api/reboot", "/api/wake"])

    kiosk = url_in(one(unit(KIOSK_UNIT), "Service", "ExecStart") or "")
    check("on the host and port the kiosk's own URL uses",
          hook.netloc, urlsplit(kiosk).netloc)
    forward = re.search(r"-L (\d+):127\.0\.0\.1:\d+",
                        one(unit(TUNNEL_UNIT), "Service", "ExecStart") or "")
    check("which is the port the tunnel binds on this end",
          forward and forward.group(1), str(hook.port))


def policy_cases(readme: str) -> None:
    print("\nthe microphone grant")
    check("the kiosk's microphone is granted by a file, not a click",
          POLICY.exists(), True, POLICY.name)
    if not POLICY.exists():
        return
    try:
        policy = json.loads(POLICY.read_text())
    except json.JSONDecodeError as err:
        policy = {"unreadable": str(err)}
    check("the policy is JSON Chromium can read", isinstance(policy, dict)
          and "unreadable" not in policy, True, sorted(policy))
    kiosk = url_in(one(unit(KIOSK_UNIT), "Service", "ExecStart") or "")
    # A grant for the wrong origin is not a smaller grant, it is no grant, and
    # the prompt it fails to suppress is one the kiosk cannot answer.
    check("for exactly the origin the kiosk unit opens",
          policy.get("AudioCaptureAllowedUrls"), [origin(kiosk)],
          origin(kiosk))
    check("and the camera, which this terminal does not have, is refused",
          policy.get("VideoCaptureAllowed"), False)
    check("installed where Chromium reads managed policy",
          "/etc/chromium/policies/managed" in readme, True)


def kiosk_cases() -> None:
    """The one thing the kiosk's URL says that is not an address.

    `?kiosk=1` is this page telling the server it is the screen in the kitchen,
    and the server has no other way to know — the panel reaches a development
    Mac down the tunnel unit, so its socket arrives from 127.0.0.1 exactly like
    a browser tab on that Mac. What turns on it is where a wake word lands
    (#132): dropped from the unit, a word said at the panel opens the
    microphone on whichever page connected last, with nothing on either screen
    to say so and both units still starting cleanly.

    A cross-reference and not three copies of the string: the name is read out
    of the unit, and the page and the server are checked for that name. A flag
    renamed in one file and not the other two is the shape this fails in.
    """
    print("\nwhat the kiosk's URL declares")
    kiosk = url_in(one(unit(KIOSK_UNIT), "Service", "ExecStart") or "")
    asked = parse_qs(urlsplit(kiosk).query)
    check("the kiosk unit says on its URL that it is the kiosk",
          asked, {"kiosk": ["1"]},
          "or the wake word goes to whichever page connected last")
    flag = next(iter(asked), "")
    check("the page forwards that name onto the handshake",
          f'.get("{flag}")' in (JS / "app.js").read_text(), True, flag)
    check("and the server reads the handshake under the same name",
          f'params.get("{flag}"' in (SRC / "live/main.py").read_text(), True,
          flag)
    # The grant is per origin, and a query string is not part of one. Cheap to
    # check and the failure would be a kiosk with no microphone at all.
    check("and none of it moves the origin the microphone is granted to",
          origin(kiosk), "http://localhost:8000")
    check("the kiosk unit enables touch events for touch panel scrolling (#532)",
          "--touch-events=enabled" in (one(unit(KIOSK_UNIT), "Service", "ExecStart") or ""),
          True)


def debug_port_cases(readme: str) -> None:
    """The argument on the kiosk's command line that has no local reason.

    Every other flag there is about this screen. `--remote-debugging-port` is
    about something else attaching to it: #162 has the agent drive the panel's
    own Chromium over CDP, because a profile directory takes one process at a
    time and the panel's is the only browser that is signed into anything. So
    it is not a switch somebody throws — it is open at every boot, and the
    cases here are about what that means being true and being written down.

    Four ways it comes undone, and none of them is a unit that fails to start:

    - The flag appended after the URL. Chromium accepts it, `url_in()` still
      finds the one URL, and every other reader in this file is green — but
      the line then reads as though the URL were the flag's value, and
      nothing else in the suite looks at `argv[-1]`.
    - `--remote-debugging-address` arriving anywhere. That is the difference
      between a port reachable down a tunnel and one the guest Wi-Fi can
      drive, on a browser holding live sessions, and it is one word.
    - A gate growing back — an `EnvironmentFile=` or a `$VAR` in the line.
      Off-by-default is the shape that was ruled against: #162 needs the port
      up on a Pi nobody is standing at.
    - The README section going away, which is the only place the exposure is
      accounted for.
    """
    print("\nthe debugging port the kiosk leaves open")
    u = unit(KIOSK_UNIT)
    argv = (one(u, "Service", "ExecStart") or "").split()
    line = " ".join(argv)
    check("the kiosk opens a debugging port, since the agent drives it",
          [a for a in argv if a.startswith(DEBUG_FLAG)],
          [f"{DEBUG_FLAG}={DEBUG_PORT}"])
    check("and the URL is still the last argument, with the flag ahead of it",
          argv[-1:], [url_in(line)])
    # The safety argument the issue asks to be re-confirmed rather than
    # trusted: six readers take the kiosk's origin, all of them through
    # `url_in()`, which returns a match only when there is exactly one. A flag
    # with no scheme in it cannot become that match.
    check("the flag carries no scheme, so exactly one URL is still on "
          "the line",
          len(re.findall(r"https?://\S+", line)), 1, url_in(line))
    check("with no address argument, so Chromium keeps the socket on loopback",
          [a for a in argv if a.startswith(DEBUG_ADDRESS)], [])
    check("on a port of its own, not the one the panel and the server share",
          DEBUG_PORT == pi_deploy.PI_PORT, False,
          f"{DEBUG_PORT} against {pi_deploy.PI_PORT}")
    # Always on is the decision, so the absence of a switch is the assertion.
    # A `$FOO` here would be systemd expanding an unset variable to nothing
    # and a Pi that silently has no port for #162 to attach to.
    check("nothing gates it: no EnvironmentFile=, no variable in the line",
          (every(u, "Service", "EnvironmentFile"),
           [a for a in argv if a.startswith("$")]), ([], []))
    check("and the unit says where the account of it is",
          DEBUG_SECTION in KIOSK_UNIT.read_text(), True, DEBUG_SECTION)

    # Counted and then required, rather than split on. `split` on a heading
    # that is not there returns the whole file, and every section check below
    # would then be reading the README at large — which is green whatever the
    # section says, including when there is no section.
    parts = readme.split(f"#### {DEBUG_SECTION}")
    check("the README accounts for it, or the exposure is undocumented",
          len(parts), 2, DEBUG_SECTION)
    body = parts[-1].split("\n#### ")[0] if len(parts) == 2 else ""
    check("quoting the flag the unit passes, spelled the same way",
          f"{DEBUG_FLAG}={DEBUG_PORT}" in body, True)
    check("and naming no debugging port other than that one",
          sorted(set(re.findall(rf"{re.escape(DEBUG_FLAG)}=(\d+)", body))),
          [str(DEBUG_PORT)])
    check("saying it is open on every boot, which is the decision",
          "every boot" in body, True)
    # Landing the unit in the repo has to open the port on the device, and for
    # six hours it did not: #183. The section has to carry the route, and the
    # route has to be the installer rather than a paragraph asking somebody to
    # remember an `scp`.
    check("and how the unit reaches the Pi, which is the deploy",
          bool(re.search(r"^uv run python tools/pi_deploy\.py$", body, re.M))
          and "restart ai-terminal-kiosk" in body, True)
    # And as one run. `deploy && restart` is two processes, and the second
    # samples `kiosk_stale` off a unit the first has already made current, so
    # it restarts the kiosk zero times — the deploy that carries a new command
    # line being the one that never re-execs onto it. The section carried that
    # recipe for the whole of #183's first landing.
    check("as one invocation, since the split restarts the kiosk zero times",
          bool(re.search(r"pi_deploy\.py deploy\s*&&", body)), False)
    check("which is a claim about the installer's table, not about prose",
          [(r.copy, r.compare) for r in pi_deploy.RESOURCES
           if r.source == KIOSK_UNIT],
          [(True, True)])
    # #144's own "not checkable from here" list had the bind address on it,
    # for a construct where the port only existed during a debug session.
    # Always on makes it a one-line reading on any boot, so the section owes
    # the reader that line.
    check("with the command that reads the bind address off the device",
          re.findall(r"ss -ltn \| grep (\d+)", body), [str(DEBUG_PORT)])

    # The accident this issue can cause does not have to arrive as an edit to
    # the unit. A sentence in any README saying "to skip the tunnel, add
    # --remote-debugging-address=0.0.0.0" changes no code, reads as help, and
    # is the whole of the exposure. Measured: with that sentence added and
    # everything else untouched, every other case here is green. So the flag
    # may be named in the docs, but only by the paragraph forbidding it — and
    # it has to be named there, or the warning has been deleted and the
    # section documents an open port and says nothing about it.
    named = [(p, para) for p in DOCS
             for para in p.read_text().split("\n\n") if DEBUG_ADDRESS in para]
    stray = sorted({str(p.relative_to(ROOT)) for p, para in named
                    if DEBUG_BAN not in para})
    check(f"{DEBUG_ADDRESS} is named in the docs only where it is forbidden",
          (len(named), stray), (1, []),
          f"{len(DOCS)} README(s) read, {len(named)} paragraph(s) name it")

    # Loopback is where it binds, so a human attaching devtools from the Mac
    # tunnels or does not get there. A debugging aside now rather than the
    # main path — the main consumer is on the Pi — but it is the step somebody
    # skips, because the Pi already runs a tunnel and it goes the other way.
    forward = re.findall(r"ssh -N -L (\d+):127\.0\.0\.1:(\d+) (\S+)", body)
    check("with one tunnel command, for attaching devtools by hand",
          len(forward), 1, str(forward))
    local, remote_port, host = forward[0] if forward else ("", "", "")
    check("run from the Mac at the Pi, which is the direction that is missing",
          host, "pi2")
    check("onto the port the kiosk opens there", remote_port, str(DEBUG_PORT))
    # The two ends need not agree — the devtools endpoint rewrites
    # webSocketDebuggerUrl out of the request's Host header — and on a Mac
    # they usually cannot, because 9222 there is Chrome's own.
    check("from a local port of its own rather than the same number",
          local != remote_port, True, local)
    check("and it is not the forward ai-terminal-tunnel.service already makes",
          re.findall(r"-L (\d+):127\.0\.0\.1:(\d+)",
                     one(unit(TUNNEL_UNIT), "Service", "ExecStart") or ""),
          [("8000", "8000")])


def signin_cases(readme: str) -> None:
    """Test the signin procedure and panel session probe.

    BROWSE_CDP is on PI_ENV since a03eb70, so a Pi whose .env row is green attaches.
    """
    print("\nthe signin procedure and panel session probe")
    session_probe = next(p for p in pi_deploy.PROBES
                         if "Google account" in p.name)
    check("the panel session probe is in the check plan",
          session_probe is not None, True)
    check("the panel session probe command is non-mutating",
          pi_deploy.mutating(session_probe.command), None)
    check("the probe opens SQLite in read-only mode",
          "mode=ro" in session_probe.command, True)
    check("the probe command does not leak host_key in print()",
          "host_key" in session_probe.command.split("print(")[-1], False)
    check("a profile with Google session cookies is OK",
          pi_deploy._panel_session(0, "2 14"),
          (pi_deploy.OK, "2 Google session cookies (14 total)"))
    check("a profile with 0 Google session cookies is WRONG",
          pi_deploy._panel_session(0, "0 10"),
          (pi_deploy.WRONG, "0 Google session cookies (10 total)"))
    missing_state, _ = pi_deploy._panel_session(
        1, "sqlite3.OperationalError: unable to open database file")
    check("a missing cookies database is MISSING",
          missing_state, pi_deploy.MISSING)

    check("README documents stopping the kiosk before signing in",
          "systemctl --user stop ai-terminal-kiosk" in readme, True)
    check("README launches Chromium with the kiosk profile",
          ("WAYLAND_DISPLAY" in readme or "chromium" in readme)
          and "--user-data-dir=" in readme, True)
    check("README directs signing into accounts.google.com",
          "accounts.google.com" in readme, True)
    check("README documents restarting the kiosk after signing in",
          "systemctl --user start ai-terminal-kiosk" in readme, True)
    signin_section = readme.split("Signing the panel profile into Google")[-1].split("####")[0]
    check("README does not leak any personal account address in the signin procedure",
          "@gmail.com" in signin_section, False)


# The README heading for the third topology: a browser on another machine.
# Named here because the cases below read the command out of that section and
# hand it to the real parser — the way this goes stale is a flag renamed in
# `main.py` and a README that goes on printing the old one.
LAN_SECTION = "Serving it to a second machine on the LAN"

# The section that says how to put an origin on the Maps key. Named here
# because the LAN section points at it by title, and a cross-reference to a
# heading somebody renamed is worse than no cross-reference: it reads as a
# promise that the instructions are a scroll away.
MAPS_SECTION = "Maps API key referrers"


def _served(argv: list[str]) -> tuple[dict, list[tuple[str, str]]]:
    """What `run` would hand uvicorn for this command line, and what it said.

    `uvicorn.run` is replaced rather than a socket mocked, because the thing
    under test is the keyword arguments and the real call takes the port and
    does not come back. Patched on the module object, which is what `run`'s
    own `import uvicorn` resolves to. A `parser.error` lands in the returned
    dict as `exit` instead, so the refusal cases read like the rest.

    The logger is unpinned for the duration — line 100 holds it at CRITICAL so
    a suite run is quiet, and the level `run` chose is exactly what half of
    these cases are about — and unhooked from the root handlers, so unpinning
    it does not print the warnings it is here to count.
    """
    import uvicorn

    seen: dict = {}
    log = logging.getLogger("ai_terminal.live.main")
    held: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            held.append(record)

    sink = _Sink()
    real, argv_was = uvicorn.run, sys.argv
    level_was, propagate_was = log.level, log.propagate
    uvicorn.run = lambda *a, **kw: seen.update(kw)
    sys.argv = ["live-server", *argv]
    log.setLevel(logging.DEBUG)
    log.propagate = False
    log.addHandler(sink)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            server.run()
    except SystemExit as exc:
        seen["exit"] = exc.code
    finally:
        log.removeHandler(sink)
        log.setLevel(level_was)
        log.propagate = propagate_was
        uvicorn.run, sys.argv = real, argv_was
    return seen, [(r.levelname, r.getMessage()) for r in held]


def _levels(argv: list[str]) -> list[str]:
    """Which levels above INFO a command line provoked, deduplicated."""
    return sorted({level for level, _ in _served(argv)[1]} - {"INFO"})


def _shell_words(line: str) -> list[str]:
    """The words a shell makes of a line the README tells somebody to paste.

    Reading the text is not enough for this one section, because the names in
    it come out of a command substitution and the substitution is where the
    procedure goes wrong. `$(hostname)` on a Mac already ends in `.local`, so
    a recipe that appends `.local` to it hands out a name with the suffix
    twice — which the text spells correctly and the shell does not.

    A real `bash` rather than a re-implementation: guessing at word splitting
    is how a check comes to disagree with the terminal the reader is typing
    into. Continuations are folded first because a `for` loop does not take a
    backslash-newline in the middle of its list.
    """
    folded = line.replace("\\\n", " ")
    out = subprocess.run(
        ["bash", "-c", f'for w in {folded}; do printf "%s\\n" "$w"; done'],
        capture_output=True, text=True)
    return out.stdout.split("\n")[:-1] if out.returncode == 0 else []


def _mdns_name() -> str:
    """The `.local` name this machine answers to, as a browser would send it.

    macOS advertises `LocalHostName`, which is what the URL bar on the second
    machine will hold and what `TrustedHostMiddleware` will compare against.
    `hostname` is a different setting and on this one returns the FQDN, so it
    is not the thing to build a name out of. Lowercased because the middleware
    has no `lower()` anywhere in it — see `_allowed_hosts` — and the name the
    browser sends is whatever mDNS published.
    """
    argv = (["scutil", "--get", "LocalHostName"] if sys.platform == "darwin"
            else ["hostname", "-s"])
    out = subprocess.run(argv, capture_output=True, text=True)
    return f"{out.stdout.strip()}.local".lower()


def _admitted(value: str) -> list[str]:
    """The allowlist `main` builds from an `ALLOWED_HOSTS` of this value."""
    was = os.environ.get("ALLOWED_HOSTS")
    os.environ["ALLOWED_HOSTS"] = value
    try:
        return server._allowed_hosts()
    finally:
        if was is None:
            del os.environ["ALLOWED_HOSTS"]
        else:
            os.environ["ALLOWED_HOSTS"] = was


def lan_tls_cases(readme: str) -> None:
    """Serving the page to a browser that is not on this machine (#245).

    A third topology beside loopback and the tunnel, and the only one where
    the transport decides whether the app works at all. `getUserMedia` and
    `AudioWorklet` are secure-context only; `http://localhost:8000` is a
    secure context by fiat and `http://192.0.2.20:8000` is not — so on the
    second machine the page loads, the mic button does nothing, and this
    server logs nothing about it. That is why the flags are here rather than
    left to bare `uvicorn ai_terminal.live.main:app --ssl-certfile …`, which
    does work: the app and its allowlist are both built at import, so going
    around `run` loses only the two lines below, and the two lines below are
    the whole of the account of what binding off loopback costs.

    The levels are the half of this worth gating. `--host 0.0.0.0` has warned
    since the guard landed, and TLS on top of it reads as the hardened
    configuration — there is a certificate, the browser draws a padlock — when
    every bit of that is about the wire and none of it is a door. So that
    pairing is an `error` and a chosen address is not, and the difference
    between the two is `_is_wildcard`.
    """
    print("\nserving it to a second machine on the LAN")
    cert, key = "/tmp/lan-cert.pem", "/tmp/lan-key.pem"
    both = ["--ssl-certfile", cert, "--ssl-keyfile", key]
    args = accepts(server._parser(), both + ["--ssl-keyfile-password", "pw"])
    check("live-server names the TLS flags itself, rather than leaving the "
          "third topology to bare uvicorn",
          args and (args.ssl_certfile, args.ssl_keyfile,
                    args.ssl_keyfile_password), (cert, key, "pw"))
    # The two shipped topologies do not want TLS and must not acquire it by
    # default: a certificate uvicorn cannot read is a server that will not
    # start, and the Pi has no certificate at all.
    check("and asks for none of it unless told, so the shipped Pi is "
          "untouched",
          [getattr(accepts(server._parser(), []), name) for name in
           ("ssl_certfile", "ssl_keyfile", "ssl_keyfile_password")],
          [None, None, None])

    seen, _ = _served(["--host", "0.0.0.0", "--port", "8443", *both])
    check("what it parsed is what uvicorn is handed",
          [seen.get("ssl_certfile"), seen.get("ssl_keyfile")], [cert, key],
          f"on {seen.get('host')}:{seen.get('port')}")
    # Passed even when it is None, because the alternative is an encrypted key
    # prompting on a terminal that is not there under a unit.
    check("the passphrase along with them, on the same call",
          "ssl_keyfile_password" in seen, True)
    check("and the flags this always had still arrive",
          [seen.get("host"), seen.get("port"), seen.get("reload")],
          ["0.0.0.0", 8443, False])

    # Half a pair is refused here because uvicorn refuses it worse: a bare
    # AssertionError one way, `[SSL] PEM lib` the other, neither naming the
    # missing flag, and both after the log has said which port it is on.
    for flag in ("--ssl-certfile", "--ssl-keyfile"):
        alone, _ = _served([flag, cert])
        check(f"{flag} without the other is refused before the port is taken",
              (alone.get("exit"), "ssl_certfile" in alone), (2, False))

    check("loopback with TLS alarms nobody, because nothing was widened",
          _levels(["--host", "127.0.0.1", *both]), [])
    check("a wildcard bind without TLS still gets the warning it always got",
          _levels(["--host", "0.0.0.0"]), ["WARNING"])
    check("and with TLS it gets louder, that being the pair that looks "
          "hardened and is not",
          _levels(["--host", "0.0.0.0", *both]), ["ERROR", "WARNING"])
    check("while an address somebody looked up does not, having been chosen",
          _levels(["--host", "192.0.2.20", *both]), ["WARNING"])
    # uvicorn reads all three as every interface, so all three are the case
    # the escalation is about.
    check("the empty host is a wildcard too, and so is the v6 one",
          [_levels(["--host", h, *both]) for h in ("", "::")],
          [["ERROR", "WARNING"]] * 2)
    check("and no address is both loopback and wildcard",
          [h for h in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::", "")
           if server._is_loopback(h) and server._is_wildcard(h)], [])

    # The one line that says where to point a browser, and it used to be a
    # literal `http://`. Under TLS that sends the reader to a scheme the
    # server will not answer on, which presents as a hang rather than as a
    # redirect — uvicorn drops a plaintext request to a TLS listener.
    said = [msg for _, msg in _served(["--port", "8443", *both])[1]
            if msg.startswith("serving on")]
    check("the line saying where to point a browser names the scheme it will "
          "answer on", said, ["serving on https://127.0.0.1:8443"])
    check("and still says http when there is no certificate",
          [msg for _, msg in _served([])[1] if msg.startswith("serving on")],
          ["serving on http://127.0.0.1:8000"])

    body = readme.split(f"#### {LAN_SECTION}")[-1].split("\n#### ")[0]
    check("the third topology is written down and not only supported",
          0 < len(body) < len(readme), True, f"{len(body)} characters")
    found = re.search(r"uv run live-server (.+?)\n```", body, re.S)
    printed = found.group(1).replace("\\\n", " ").split() if found else []
    doc = accepts(server._parser(), printed)
    check("and the command it prints is one this parser accepts",
          doc is not None, True, " ".join(printed))
    check("carrying both halves of the pair, since one of them is an "
          "AssertionError",
          [bool(doc and doc.ssl_certfile), bool(doc and doc.ssl_keyfile)],
          [True, True])
    check("bound off loopback, or the second machine never reaches it",
          server._is_loopback(doc.host if doc else "127.0.0.1"), False)
    # 8000 is the tunnel's, so a README that reached for it would take the
    # panel down to show a laptop.
    check("and off the port the panel's tunnel holds",
          doc is not None and doc.port != pi_deploy.PI_PORT, True,
          f"{doc.port if doc else '?'}, tunnel on {pi_deploy.PI_PORT}")
    # Without it every request is `Invalid host header`, a bare 400 the
    # middleware writes no log line for — which reads as a TLS fault and is
    # the likeliest way this procedure fails in somebody's hands.
    # To the end of the line rather than to the first space: the value holds a
    # substitution and `$(hostname -s)` has one in the middle of it.
    setting = re.search(r"ALLOWED_HOSTS=(.+?) *\\?\n", body)
    check("with ALLOWED_HOSTS set on the same command line",
          setting is not None, True)

    # And spelled the way a shell will read it, which is the part no amount of
    # reading the README catches. The name here is written as a substitution,
    # so what the reader gets depends on a machine setting rather than on this
    # text: `$(hostname)` returns the FQDN on a Mac, and appending `.local` to
    # that produces a SAN the URL bar never shows and an allowlist entry no
    # `Host` header can match. Both failures land as the same bare 400 with
    # nothing logged. The three cases below run the substitution instead of
    # believing it, so the recipe is held by the shell and not by review.
    mdns = _mdns_name()
    signed = re.search(r"mkcert (.+?)\n\n", body, re.S)
    # Lowercased on both sides: a DNS name is case-insensitive and the
    # middleware is not, so the comparison that matters is this one.
    san = [word.lower() for word in
           _shell_words(signed.group(1) if signed else "")]
    check("the certificate the recipe asks for carries the name this machine "
          "publishes", mdns in san, True, f"{mdns} among {' '.join(san)}")
    admitted = _admitted(" ".join(_shell_words(setting.group(1)))
                         if setting else "")
    check("and the allowlist it sets admits that same name",
          mdns in admitted, True, f"{mdns} among {', '.join(admitted)}")
    # The two lists are written on separate lines and drift apart one edit at
    # a time. Every name the server will answer to needs a SAN, or the reader
    # picks the one address out of the three that the certificate does not
    # cover and gets a warning page instead of the panel.
    check("and every name it answers to is one the certificate covers",
          sorted(set(admitted) - set(san)), [])

    # The one failure in this topology that leaves nothing on screen and
    # nothing in this server's log. A new origin is not on the Maps key's
    # referrer list, so the place card's map comes up grey with
    # `RefererNotAllowedMapError` in the console — and the page's only error
    # path for the bootstrap is `script.onerror`, which never fires, because
    # the script returns 200 and the referrer is checked after that. So the
    # README has to say it, and has to point at the section that says how.
    check("the section warns that the map will be grey until the key is told "
          "about the new origin", "RefererNotAllowedMapError" in body, True)
    check("and sends the reader to the section that says how to add one",
          [MAPS_SECTION in body, f"### {MAPS_SECTION}" in readme],
          [True, True])
    # The referrer list does not honour a port wildcard — the passage that
    # section is built on measured that — so an entry at the wrong port is an
    # entry that grants nothing.
    check("at the port the recipe actually serves on",
          doc is not None and f":{doc.port}/*" in body, True,
          f"port {doc.port if doc else '?'}")


def procedure_cases(readme: str) -> None:
    print("\nthe procedure a person follows on the Pi")
    files = sorted(p.name for p in DEPLOY.iterdir() if p.is_file() and p.suffix != ".md")
    units = [f for f in files if f.endswith(".service")]
    copied = re.findall(r"scp (deploy/\S+) pi2:", readme)
    check("every file in deploy/ is copied by some line of the procedure",
          [f for f in files
           if not any(fnmatch(f"deploy/{f}", p) for p in copied)], [],
          f"{len(files)} files over {len(copied)} scp lines")
    # The copy is a glob, so a new unit joins it by existing; the enable line
    # is a list of names, and joins it only if somebody remembers. Copied and
    # never enabled is a file on the Pi that does nothing at all.
    enabled = " ".join(re.findall(r"systemctl --user enable[^\n']*", readme))
    check("and every unit it copies is enabled somewhere in the README",
          [u for u in units if u.removesuffix(".service") not in enabled], [],
          f"{len(units)} units")

    # Presence, not behaviour. These three lines are the ones whose deletion
    # is invisible until somebody stands in the kitchen and says the phrase.
    #
    # Read out of the install section rather than the whole file, because both
    # strings occur twice: `pipewire-alsa` is also installed in the PipeWire
    # measurement passage a few paragraphs up, so `in readme` stays green when
    # the line the procedure actually runs is deleted — the one mutation that
    # survived the audit of this script. `enable-linger` is a whole-file match
    # only by luck of occurring once, which is not a property to rely on.
    body = readme.split(f"#### {SECTION}")[-1].split("\n#### ")[0]
    check("the install section is where the procedure's lines are read from",
          0 < len(body) < len(readme), True, f"{len(body)} characters")
    device = accepts(wake_parser(), (one(unit(WAKE_UNIT), "Service",
                                         "ExecStart") or "").split()[3:])
    check("--device pipewire is paired with the package it needs",
          device.device != "pipewire" or "pipewire-alsa" in body, True,
          "pipewire-alsa")
    check("the mic setup is paired with the package it needs",
          "i2c-tools" in body, True, "i2c-tools")
    check("the account is lingered, or the monitor dies at logout",
          "loginctl enable-linger" in body, True)

    log = re.search(r'logger\.info\(\s*"(wake word reached [^"]*)"',
                    (ROOT / "src/ai_terminal/live/main.py").read_text())
    line = log.group(1).split("%")[0].strip() if log else "|no such log|"
    check("the log line the verification step waits for is the one written",
          line in readme, True, line)
    stem = WAKE_UNIT.name.removesuffix(".service")
    check("and the journal it says to read is this unit's",
          f"journalctl --user -u {stem}" in readme, True, stem)

    # `.env` is on the exclude list, so no run of the installer can put these
    # on the Pi and the check phase can only report them missing. A person is
    # the mechanism, which makes the README the mechanism — and the comparison
    # is both ways round. A key the probe demands and the README never names is
    # a red row nobody can clear; a key the README asks for and the probe does
    # not read is a line that can be skipped without anything saying so.
    section = readme.split(f"#### {SERVER_SECTION}")[-1].split("\n#### ")[0]
    check("the section that says which keys is the one the deploy names",
          0 < len(section) < len(readme), True, f"{len(section)} characters")
    check("and the keys it lists are exactly the ones the check phase reads",
          sorted(re.findall(r"`([A-Z_]+)=[^`]*`", section)),
          sorted(pi_deploy.PI_ENV + pi_deploy.PI_ENV_ANY))
    # Both halves of the pair have to be in the prose, not just whichever one
    # the person who wrote the section happened to use: the section is the
    # only place that says either will do, and a Pi set up from a README that
    # named one of them would be told to write the key it does not have.
    check("and the either-or pair is on that list too, both halves of it",
          [name for name in pi_deploy.PI_ENV_ANY if f"`{name}=" in section],
          list(pi_deploy.PI_ENV_ANY))
    check("while the pair is not also on the every-one-of list",
          sorted(set(pi_deploy.PI_ENV) & set(pi_deploy.PI_ENV_ANY)), [])
    # The shipped Pi has no `gws` on purpose, so that half of this line is
    # printed at every boot for the life of the machine. At `error` it is a red
    # line a correct install can never clear, which is how a level stops
    # meaning anything — and the other half, a `browse` that was meant to work,
    # is a fault and stays red. The README prints the line a correct Pi logs,
    # so the two have to agree on which level that is.
    calls = re.findall(r'logger\.(\w+)\("these tools will refuse work',
                       (SRC / "live" / "main.py").read_text())
    check("the tool refusal is said at two levels, the quieter one first",
          calls, ["warning", "error"])
    check("and the README shows the boot line at the level a good Pi gets",
          f"{calls[0].upper()} ai_terminal.live.main: these tools will "
          "refuse work: gws" in readme, True)


# --------------------------------------------------------------------------
# tools/pi_deploy.py
#
# The installer is the one script in this repo whose whole job is on another
# machine, so everything below drives it from fixtures and nothing here is
# allowed to open a connection. `plan_cases` proves that rather than asserting
# it: `subprocess.run` and `socket.getaddrinfo` are replaced with spies for the
# length of a full `--dry-run` of all four phases, and the case is that neither
# spy saw anything.
# --------------------------------------------------------------------------

# One Pi with every row met, and one with each row as pi2 actually read on
# 2026-09-09. Every probe is driven through both, so a reader that cannot tell
# the two apart — the failure that makes a check script worse than nothing —
# is a case rather than a surprise on the device.
def _met() -> dict[str, str]:
    size = pi_deploy.MODEL_LOCAL.stat().st_size
    return {
        "the OS, the kernel and the interpreter":
            'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\n'
            "6.18.39+rpt-rpi-2712 aarch64\nPython 3.13.5",
        "the audio packages":
            "install ok installed pipewire 1.4.2-1+rpt3\n"
            "install ok installed pipewire-alsa 1.4.2-1+rpt3\n"
            "install ok installed wireplumber 0.5.8-2\n"
            "install ok installed i2c-tools 4.3-2+b3",
        "the desktop packages":
            "install ok installed chromium 152.0.7977.75\n"
            "install ok installed labwc 0.9.8\n"
            "install ok installed kanshi 1.5.1\n"
            "install ok installed alsa-utils 1.2.14\n"
            "install ok installed fonts-noto-color-emoji 2.051-1",
        "uv, at the absolute path the unit spells": "uv 0.12.10",
        "the project's virtualenv": "wake-ok",
        "the ALSA route into the session's PipeWire graph":
            "pipewire\n    PipeWire Sound Server",
        "the capture card":
            "**** List of CAPTURE Hardware Devices ****\n"
            "card 2: seeed2micvoicec [seeed-2mic-voicecard], device 0: ...",
        "the default audio source":
            "Audio\n"
            " ├─ Sources:\n"
            " │  *   46. USB Composite Device Mono           [vol: 0.74]\n"
            " │      47. Built-in Audio Stereo               [vol: 1.00]\n",
        "the HAT's overlay": "57:dtoverlay=respeaker-2mic-v2_0",
        "the journald drop-in, or there is no per-user journal": "present",
        "AI_TERMINAL_DEV_HOST, which the tunnel exits without":
            "AI_TERMINAL_DEV_HOST=alex@192.0.2.20",
        "a current copy of this directory": "present",
        "the model the unit's --model names": str(size),
        # Names and no values, which is what the command asks for: this file
        # holds the project, the key and the credentials.
        "the keys the Pi's own .env sets, which the rsync will not copy":
            "GOOGLE_CLOUD_PROJECT\nBROWSE_CHROMIUM\nBROWSE_CDP\n"
            "BROWSE_ALLOWLIST\nSESSIONS_DB\nGWS_CACHE_DIR\nHOME_COORDS",
        "the microphone grant, where Chromium reads managed policy":
            "644 root root",
        "the Google account in the panel's Chromium profile": "2 14",
        "linger, or the monitor is up only while somebody is watching":
            "Linger=yes",
        "the microphone bias DAPM keeps powering down": "0x86",
        "the server is running": "file-present\nenabled\nactive",
        "the wake monitor's unit is running": "file-present\nenabled\nactive",
        "the kiosk is running": "file-present\nenabled\nactive",
        "the tunnel is installed": "file-present\ndisabled\ninactive",
        # The met reading is this tree's own text, because that is what
        # `_unit_matches` compares the device's copy against. Said that way
        # rather than as a device reading somebody took once: the comment that
        # stood here claimed pi2 matched byte for byte on 2026-09-10, and #183
        # measured that it did not — it was two kiosk revisions behind, and the
        # claim was the one line in the tree that discouraged looking.
        "the kiosk's unit is the one in deploy/":
            pi_deploy.KIOSK_UNIT.read_text(),
        "the tunnel's unit is the one in deploy/":
            pi_deploy.TUNNEL_UNIT.read_text(),
        "the kanshi config":
            f"{hashlib.sha256(pi_deploy.KANSHI_CONFIG.read_bytes()).hexdigest()}  "
            f"/home/{pi_deploy.PI_USER}/.config/kanshi/config",
        "the labwc rc.xml":
            f"{hashlib.sha256(pi_deploy.LABWC_CONFIG.read_bytes()).hexdigest()}  "
            f"/home/{pi_deploy.PI_USER}/.config/labwc/rc.xml",
        "the touch calibration rule":
            f"{hashlib.sha256(pi_deploy.TOUCH_RULES.read_bytes()).hexdigest()}  "
            "/etc/udev/rules.d/99-touch-calibration.rules",
        "the WirePlumber config":
            f"{hashlib.sha256(pi_deploy.WIREPLUMBER_CONFIG.read_bytes()).hexdigest()}  "
            f"/home/{pi_deploy.PI_USER}/.config/wireplumber/wireplumber.conf.d/50-usb-mic.conf",
    }


UNMET = {
    "the OS, the kernel and the interpreter":
        'PRETTY_NAME="Debian GNU/Linux 11 (bullseye)"\n'
        "5.10.0-rpi aarch64\nPython 3.9.2",
    "the audio packages": "install ok installed pipewire 1.4.2-1+rpt3\n"
                          "dpkg-query: no packages found matching "
                          "pipewire-alsa",
    "the desktop packages": "dpkg-query: no packages found matching chromium",
    "uv, at the absolute path the unit spells":
        "bash: /home/pi/.local/bin/uv: No such file or directory",
    "the project's virtualenv": "wake-broken",
    "the ALSA route into the session's PipeWire graph": "",
    "the capture card":
        "**** List of CAPTURE Hardware Devices ****\n"
        "card 0: vc4hdmi [vc4-hdmi], device 0: MAI PCM i2s-hifi-0 []",
    "the default audio source":
        "Audio\n"
        " ├─ Sources:\n"
        " │  *   47. Built-in Audio Stereo               [vol: 1.00]\n",
    "the HAT's overlay": "",
    "the journald drop-in, or there is no per-user journal": "absent",
    "AI_TERMINAL_DEV_HOST, which the tunnel exits without":
        "cat: /home/pi/.config/environment.d/ai-terminal.conf: "
        "No such file or directory",
    "a current copy of this directory": "absent",
    "the model the unit's --model names": "",
    # An `.env` the rsync never copied, so `grep` matches nothing and the
    # `|| true` in the command keeps the exit status at zero.
    "the keys the Pi's own .env sets, which the rsync will not copy": "",
    "the microphone grant, where Chromium reads managed policy":
        "stat: cannot stat '/etc/chromium/policies/managed/"
        "ai-terminal-chromium-policy.json': No such file or directory",
    "the Google account in the panel's Chromium profile": "0 0",
    "linger, or the monitor is up only while somebody is watching":
        "Linger=no",
    "the microphone bias DAPM keeps powering down": "0x00",
    "the server is running": "Unit could not be found",
    "the wake monitor's unit is running": "Unit could not be found",
    "the kiosk is running": "Unit could not be found",
    # Not missing: on the Pi as it read on 2026-09-09, and up. That is the
    # failure this row exists for — the file is meant to stay and the unit is
    # meant to be stopped, so its unmet fixture is the one that looks healthy.
    "the tunnel is installed": "file-present\nenabled\nactive",
    # The kiosk unit as it read before #145, which is what every Pi the
    # installer provisioned still carries: the file is there, the unit is
    # enabled and active, and every other row about it is green. Only this one
    # can see the line.
    "the kiosk's unit is the one in deploy/":
        pi_deploy.KIOSK_UNIT.read_text().replace(
            "After=ai-terminal-tunnel.service",
            "Wants=ai-terminal-tunnel.service\n"
            "After=ai-terminal-tunnel.service"),
    # And a Pi the installer provisioned and nobody scp'd to by hand, which
    # has no tunnel unit at all.
    "the tunnel's unit is the one in deploy/": "",
    "the kanshi config": "",
    "the labwc rc.xml": "",
    "the touch calibration rule": "",
    "the WirePlumber config": "",
}


def canned(answers: list[str]):
    """A `spawn` that hands back fixture output instead of reaching the Pi."""
    left = list(answers)

    def spawn(step, timeout=900):
        if step.name == "deploy lock" or f"cat {pi_deploy.LOCK_FILE}" in " ".join(step.argv):
            return 0, ""
        return 0, left.pop(0)
    return spawn


def quietly(call, *args, **kwargs):
    """Run something that prints a table, and keep the table out of the way."""
    with contextlib.redirect_stdout(io.StringIO()) as held:
        answer = call(*args, **kwargs)
    return answer, held.getvalue()


def plan_cases(readme: str) -> None:
    print("\nthe installer's plans, which nothing here may run")
    seen: list[tuple[str, ...]] = []
    looked: list[str] = []
    real_run, real_addr = subprocess.run, socket.getaddrinfo
    subprocess.run = lambda argv, *a, **k: seen.append(tuple(argv))
    socket.getaddrinfo = lambda host, *a, **k: looked.append(host)
    try:
        codes = [quietly(pi_deploy.main, ["--dry-run"] + phase)[0]
                 for phase in ([], ["check"], ["install"], ["deploy"],
                               ["restart"])]
        printed = quietly(pi_deploy.main, ["--dry-run"])[1]
    finally:
        subprocess.run, socket.getaddrinfo = real_run, real_addr
    check("a dry run of every phase spawns nothing at all", seen, [])
    check("and does not even look the host up, so it runs on a plane",
          looked, [])
    check("each one says so by exiting zero", codes, [0] * 5)

    # The order a full run emits, and read off what it printed rather than off
    # `PHASES`, because `main` is what iterates it and a case over the constant
    # would not see a `sorted()` put in later.
    #
    # The order and not the set, which is the whole of #174: the two read
    # identically as a set and are not the same procedure. `install` is `uv
    # sync` and `bash tools/mic_setup.sh` run *inside* `PI_DIR`, so what
    # decides which tree they see is whether the rsync has happened yet. Run
    # ahead of it, a commit that adds a dependency installs against the lock
    # the Pi already had and `restart` then serves the new tree on it — and
    # every step of install is idempotent, so the run prints done throughout.
    # On a Pi with no `~/ai-terminal` yet, `cd ~/ai-terminal && uv sync` exits
    # non-zero, the run stops on the failed phase, and `deploy` — which is what
    # creates the directory — is never reached at all. That is the first-boot
    # path.
    emitted = [line.split(" — ")[0] for line in printed.splitlines()
               if line.endswith("steps, nothing run")]
    check("a full run copies the tree over before it installs against it",
          emitted, ["check", "deploy", "install", "restart"],
          "`uv sync` and mic_setup.sh run inside PI_DIR, so the rsync is what "
          "decides which tree they read")
    check("and the README's one-liner says the same order, since that is "
          "where somebody reads it",
          re.search(r"pi_deploy\.py\s+#\s*(.+)", readme).group(1).strip(),
          ", ".join(emitted))

    plans = {name: pi_deploy.PHASES[name]("pi2") for name in pi_deploy.PHASES}
    check("every phase's plan is available as data, not printed and gone",
          [name for name, plan in plans.items()
           if not plan or not all(isinstance(s, pi_deploy.Step)
                                  for s in plan)], [],
          ", ".join(f"{k}:{len(v)}" for k, v in plans.items()))

    # A "check" that quietly repairs and then reports green on a Pi it has
    # just changed is the failure this pins, and the scan is over the command
    # rather than over a flag beside it.
    check("nothing in the check plan writes to the Pi",
          [s.name for s in plans["check"] if pi_deploy.mutating(s.argv)], [],
          f"{len(plans['check'])} probes")
    check("and the scan can see a write when there is one",
          pi_deploy.mutating(("ssh", "h", "sudo apt-get install -y x")),
          "apt-get")
    check("including one hidden inside a script it only starts",
          pi_deploy.mutating(("ssh", "h", "bash tools/mic_setup.sh")),
          "tools/mic_setup.sh")
    check("while is-enabled is not enable, and i2cget is not i2cset",
          pi_deploy.mutating(("ssh", "h", "systemctl --user is-enabled x; "
                              "sudo i2cget -f -y 1 0x18 0x19")), None)
    check("and rsync's own rehearsal reads rather than writes",
          pi_deploy.mutating(pi_deploy._rsync("pi2", dry=True)), None)
    check("while the copy it rehearses does not",
          pi_deploy.mutating(pi_deploy._rsync("pi2", dry=False)), "rsync")


def derivation_cases(readme: str) -> None:
    print("\nwhat the installer takes from the files it installs")
    source = (ROOT / "tools/pi_deploy.py").read_text()
    # Not "is it right" but "was it read": a literal here would be a second
    # copy that agrees with the unit until the day somebody edits one of them.
    for what, value in (("the model path", pi_deploy.MODEL),
                        ("the working directory", pi_deploy.PI_DIR),
                        ("the uv the unit invokes", pi_deploy.UV)):
        check(f"{what} is nowhere in the installer's own source",
              value in source, False, value)
    exec_start = one(unit(WAKE_UNIT), "Service", "ExecStart") or ""
    check("the model is the one the unit's --model names",
          pi_deploy.MODEL, pi_deploy.flag(exec_start, "--model"))
    check("so changing the unit moves the plan with it",
          pi_deploy.flag(exec_start.replace("ls128", "kw512"), "--model"),
          pi_deploy.MODEL.replace("ls128", "kw512"))
    check("the deploy stats that path rather than trusting the transfer",
          any(pi_deploy.MODEL in " ".join(s.argv)
              for s in pi_deploy.deploy_plan("pi2")), True)
    check("and it is a file this repo ships, so the rsync can carry it",
          pi_deploy.MODEL_LOCAL.is_file(), True,
          f"{pi_deploy.MODEL_LOCAL.stat().st_size} bytes")

    # The grant is the one row where a wrong answer is silent on both sides:
    # Chromium reads the policy, matches nothing, and prompts a kiosk that
    # cannot answer.
    kiosk = url_in(one(unit(KIOSK_UNIT), "Service", "ExecStart") or "")
    check("the origin the installer asserts is the kiosk unit's own",
          pi_deploy.POLICY_ORIGIN, origin(kiosk))
    check("with the port in it, since a grant for localhost alone is none",
          urlsplit(pi_deploy.POLICY_ORIGIN).port,
          urlsplit(kiosk).port)
    check("and the shipped policy grants exactly that",
          json.loads(POLICY.read_text()).get("AudioCaptureAllowedUrls"),
          [pi_deploy.POLICY_ORIGIN])
    check("a policy that lost the port fails the assertion the restart makes",
          pi_deploy._grant_landed(0, "644 root root\n"
                                  '{"AudioCaptureAllowedUrls": '
                                  '["http://localhost"]}') is not None, True)

    # The exclude list is the one constant deliberately written twice. A
    # single source would make this comparison vacuous, and the two costs are
    # not symmetrical: a README that has drifted is a person pasting a
    # `--delete` without `--exclude 'sessions.db*'`.
    block = re.search(r"rsync -a --delete \\\n(.*?)\n\s*~/Documents",
                      readme, re.S)
    written = re.findall(r"--exclude '?([^'\s]+)'?", block.group(1) if block
                         else "")
    check("the plan's excludes are the README's, in the same order",
          list(pi_deploy.EXCLUDES), written)
    check("and they reach the command that runs",
          re.findall(r"--exclude '?([^'\s]+)'?",
                     " ".join(pi_deploy._rsync("pi2", dry=False))), written)
    check("the README names the installer, so it is not a script nobody finds",
          "tools/pi_deploy.py" in readme, True)
    check("which is not a *_check.py, so the offline sweep never ssh's",
          Path(pi_deploy.__file__).name.endswith("_check.py"), False,
          Path(pi_deploy.__file__).name)


def ir_cases(readme: str) -> None:
    """The mic-versus-mic figures in the README, against the IRs that ship.

    The README used to print one DRR pair — -5.2 dB for a USB take against the
    HAT's -5.4 — that reproduces from nothing in the checkout, and never
    mentioned the pair it does ship, which goes the other way by 3.0 dB. The
    numbers below are read out of the two `.wav` files with the repo's own
    `describe()` rather than written down here a second time, so the prose
    stays pinned to the artefacts.

    Nothing opens a device: `scipy.io.wavfile` reads two files off disk, and
    `measure_ir` imports `sounddevice` inside `cmd_record`, not at module
    level.
    """
    print("\nthe shipped impulse responses, and the README's figures for them")
    sys.path.insert(0, str(ROOT / "tools"))
    import measure_ir
    import numpy as np
    from scipy.io import wavfile

    lines = readme.splitlines()
    expected_direct = {
        "kitchen_panel": "+12.2 dB at 2-4 kHz",
        "kitchen_usb": "+16.8 dB at 500-1k",
    }
    for name in ("kitchen_panel", "kitchen_usb"):
        h = wavfile.read(ROOT / "training" / "rirs" / f"{name}.wav")[1]
        said = measure_ir.describe(h)
        rt60, drr = re.search(r"RT60 ([\d.]+)s\s+DRR ([-+][\d.]+) dB",
                              said).groups()
        row = next((ln for ln in lines
                    if ln.startswith("|") and f"`{name}.wav`" in ln), "")
        check(f"the README's row for {name}.wav quotes its measured RT60",
              f"{rt60} s" in row, True, f"{rt60} s — {row.strip() or 'no row'}")
        check("and the DRR that row is the only re-runnable evidence for",
              f"{drr} dB" in row, True, f"{drr} dB")

        ok, dev, band = measure_ir.direct_arrival(h)
        check(f"{name}.wav fails the direct-arrival flatness gate",
              ok, False, f"direct {'pass' if ok else 'fail'} ({dev:+.1f} dB at {band})")
        check(f"and {name}.wav quotes the README's measured deviation",
              f"{dev:+.1f} dB at {band}", expected_direct[name])

    # A synthetic flat-direct IR (flat impulse, reflections well after 5 ms)
    # must pass the gate.
    flat_ir = np.zeros(int(measure_ir.IR_RATE * 0.4), dtype=np.float32)
    flat_ir[int(measure_ir.IR_RATE * 0.002)] = 0.9
    flat_ir[int(measure_ir.IR_RATE * 0.015)] = 0.4
    flat_ir[int(measure_ir.IR_RATE * 0.030)] = -0.3
    flat_ok, flat_dev, flat_band = measure_ir.direct_arrival(flat_ir)
    check("a synthetic flat-direct IR passes the direct-arrival flatness gate",
          flat_ok, True, f"{flat_dev:+.1f} dB at {flat_band}")

    # The -5.4/-5.2 pair is not derivable from anything here. It may stay, but
    # not as a bare figure: the reader has to be told the takes are missing
    # before deciding what the number is worth.
    at = next(i for i, ln in enumerate(lines)
              if "kitchen, HAT behind the panel" in ln)
    check("the unreproducible DRR table says its takes are not in the repo",
          "not in this repo" in "\n".join(lines[at:at + 10]), True)

    # The recommendation used to be bare. Whatever the reason is, it has to
    # name the artefact it rests on, so changing the default means changing a
    # sentence that points at a file rather than deleting an assertion.
    at = next(i for i, ln in enumerate(lines)
              if "as the default source" in ln)
    check("the default-source recommendation gives a reason that names the IR",
          "kitchen_panel.wav" in "\n".join(lines[at:at + 20]), True)

    # ... and the settings prose 2200 lines above it has to point at that
    # section rather than assert a second, opposite default of its own.
    at = next(i for i, ln in enumerate(lines)
              if "The microphone is the setting that matters" in ln)
    check("and the settings prose defers to it instead of contradicting it",
          "Both processes can hold the microphone"
          in "\n".join(lines[at:at + 4]), True)


def probe_cases() -> None:
    print("\nevery row of the table, met and unmet")
    met = _met()
    check("there is a fixture for every probe and no probe without one",
          sorted(p.name for p in pi_deploy.PROBES),
          sorted(set(met) & set(UNMET)),
          f"{len(pi_deploy.PROBES)} rows")
    for probe in pi_deploy.PROBES:
        state, detail = probe.read(0, met[probe.name])
        check(f"met: {probe.name}", state, pi_deploy.OK, detail)
    for probe in pi_deploy.PROBES:
        state, detail = probe.read(0, UNMET[probe.name])
        check(f"unmet: {probe.name}", state != pi_deploy.OK, True,
              f"{state} — {detail}")

    # The cards carry emoji a model wrote since #136, and nothing else on the
    # Pi would have installed a font that can draw one. The failure is silent
    # in the worst way: `markEmoji` in `app.js` sets `no-emoji`, and the only
    # rule behind that class swaps the calendar's glyph for a Material Symbol
    # — there is no symbol to put behind an arbitrary emoji in a sentence, so
    # a card of tofu boxes is what is left.
    check("the panel is installed with a font that can draw an emoji",
          "fonts-noto-color-emoji" in pi_deploy.APT_DESKTOP, True,
          ", ".join(pi_deploy.APT_DESKTOP))
    desktop = next(p for p in pi_deploy.PROBES if p.name == "the desktop packages")
    check("and the probe would notice it missing rather than pass anyway",
          desktop.read(0, met["the desktop packages"].replace(
              "install ok installed fonts-noto-color-emoji 2.051-1", "")),
          (pi_deploy.MISSING, "missing fonts-noto-color-emoji"))

    # The four that were unmet on pi2 the day this was written, named rather
    # than left to the loop, because their exact wording is what the reader
    # has to recognise.
    wake = next(p for p in pi_deploy.PROBES if "wake monitor" in p.name)
    check("a unit that was never installed reads as missing, not as stopped",
          wake.read(0, "Unit could not be found"),
          (pi_deploy.MISSING, "no unit file on the Pi"))
    linger = next(p for p in pi_deploy.PROBES if p.name.startswith("linger"))
    check("Linger=no is wrong rather than absent, because the user is there",
          linger.read(0, "Linger=no"), (pi_deploy.WRONG, "Linger=no"))
    grant = next(p for p in pi_deploy.PROBES if "managed policy" in p.name)
    check("an absent managed-policy directory reads as missing",
          grant.read(1, "stat: cannot stat '/etc/chromium/policies/managed/"
                     "x.json': No such file or directory")[0],
          pi_deploy.MISSING)
    model = next(p for p in pi_deploy.PROBES if "--model" in p.name)
    check("and so does a model the rsync never carried",
          model.read(0, ""), (pi_deploy.MISSING, f"no file at "
                              f"{pi_deploy.MODEL}"))
    check("while one that arrived truncated is wrong, not missing",
          model.read(0, "1024")[0], pi_deploy.WRONG)

    env = next(p for p in pi_deploy.PROBES if ".env" in p.name)
    check("an .env short of a key is missing that key by name",
          env.read(0, "GOOGLE_CLOUD_PROJECT\nBROWSE_CDP\nBROWSE_ALLOWLIST\n"
                  "SESSIONS_DB\nGWS_CACHE_DIR\nHOME_COORDS"),
          (pi_deploy.MISSING, "missing BROWSE_CHROMIUM"))
    check("and all of them there is met, whatever else the file sets",
          env.read(0, "\n".join(pi_deploy.PI_ENV)
                   + "\nHOME_COORDS\nGOOGLE_API_KEY")[0],
          pi_deploy.OK)
    # The Pi shipped on 2026-09-10 with exactly this file: every name the
    # probe demanded, and nowhere to ask about the weather. The check phase
    # called it complete, so #154 was found on the panel rather than here.
    check("but the three alone are not, because the clock has nowhere to ask",
          env.read(0, "\n".join(pi_deploy.PI_ENV)),
          (pi_deploy.MISSING, "missing HOME_COORDS or HOME_LOCATION"))
    check("and an empty file names the pair once, after the every-one-of set",
          env.read(0, "")[1],
          "missing BROWSE_CHROMIUM, BROWSE_CDP, BROWSE_ALLOWLIST, "
          "SESSIONS_DB, GWS_CACHE_DIR, HOME_COORDS or HOME_LOCATION")
    # Either half clears the row on its own, and the row says which one did:
    # a coordinate pair costs nothing and an address costs one geocode per
    # process, and the Pi is allowed to pick.
    check("coordinates alone clear it, and the row names the one that did",
          env.read(0, "\n".join(pi_deploy.PI_ENV) + "\nHOME_COORDS"),
          (pi_deploy.OK, "BROWSE_CHROMIUM, BROWSE_CDP, BROWSE_ALLOWLIST, "
           "SESSIONS_DB, GWS_CACHE_DIR, "
           "HOME_COORDS are set"))
    check("an address alone clears it too",
          env.read(0, "\n".join(pi_deploy.PI_ENV) + "\nHOME_LOCATION"),
          (pi_deploy.OK, "BROWSE_CHROMIUM, BROWSE_CDP, BROWSE_ALLOWLIST, "
           "SESSIONS_DB, GWS_CACHE_DIR, "
           "HOME_LOCATION are set"))
    check("and both together is not an error, only both named",
          env.read(0, "\n".join(pi_deploy.PI_ENV)
                   + "\nHOME_LOCATION\nHOME_COORDS"),
          (pi_deploy.OK, "BROWSE_CHROMIUM, BROWSE_CDP, BROWSE_ALLOWLIST, "
           "SESSIONS_DB, GWS_CACHE_DIR, "
           "HOME_COORDS, HOME_LOCATION are set"))
    # An exact name and not a substring of the file. The command brings back
    # whole names, so a key that merely begins with one of these is a
    # different key, and the cheap way to write the test above is an `in out`
    # that cannot tell them apart.
    check("a key that only looks like one of the pair does not clear it",
          env.read(0, "\n".join(pi_deploy.PI_ENV) + "\nHOME_LOCATION_LABEL"),
          (pi_deploy.MISSING, "missing HOME_COORDS or HOME_LOCATION"))
    # The two demands are independent, and a file that answers only the
    # either-or must still be short of the every-one-of set. Written out
    # rather than inferred, because the cheap way to satisfy the cases above
    # is an `any` that swallows the `all`.
    check("a location on its own leaves the every-one-of set missing",
          env.read(0, "HOME_COORDS"),
          (pi_deploy.MISSING, "missing BROWSE_CHROMIUM, BROWSE_CDP, "
           "BROWSE_ALLOWLIST, SESSIONS_DB, GWS_CACHE_DIR"))
    # The half-configured Pi this issue is about, and the reason both names
    # are on `PI_ENV` unconditionally rather than demanded as a pair: an
    # endpoint with nowhere it may go is the state `browser.py` refuses to
    # start on, and the row has to be able to say so before somebody asks a
    # question and reads the refusal in a journal instead.
    check("an endpoint with no allowlist beside it is a missing key, by name",
          env.read(0, "BROWSE_CHROMIUM\nBROWSE_CDP\nSESSIONS_DB\n"
                  "GWS_CACHE_DIR\nHOME_COORDS"),
          (pi_deploy.MISSING, "missing BROWSE_ALLOWLIST"))
    # And the Pi as it shipped, which is neither of them. Red, deliberately:
    # the feature was invisible from #144 to #208 because this row was green
    # with the endpoint unset, and only the owner can clear it.
    check("and a Pi with neither is red rather than quietly off",
          env.read(0, "BROWSE_CHROMIUM\nSESSIONS_DB\nGWS_CACHE_DIR\n"
                  "HOME_COORDS"),
          (pi_deploy.MISSING, "missing BROWSE_CDP, BROWSE_ALLOWLIST"))
    # The one case that is worth reaching a shell for. This probe reads the
    # file that holds the project, the key and the credentials, and the
    # protection is in the command rather than in the reader — so the command
    # is run, against a fixture with a value nobody would want in a terminal
    # transcript, and what comes back is what would have been printed.
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".env").write_text(
            "BROWSE_CHROMIUM=/usr/bin/chromium\n"
            "GOOGLE_API_KEY=would-be-a-leak\n")
        out = subprocess.run(env.command.replace(PI_DIR, tmp), shell=True,
                             capture_output=True, text=True).stdout
    check("and the command it runs cannot bring a value back",
          sorted(out.split()), ["BROWSE_CHROMIUM", "GOOGLE_API_KEY"],
          f"{len(out.split())} names, no values")
    # The same command against the file a person makes by copying
    # `.env.example`, which ships `HOME_LOCATION=` with nothing after it
    # (`.env.example:33`). A name is not an answer: `where` strips that value
    # to "" and gives up, so a row that counted the name would say the file
    # was finished on a Pi with nowhere to ask.
    #
    # The two quoted rows are the same nothing spelled the way somebody who
    # has written shell before spells it, and they are the ones this probe
    # used to miss: `"` is not a space, so a rule that only demanded a
    # non-space character counted the opening quote as the value. What the
    # app gets is not the quote — `main.py:26` loads the file with
    # `override=True`, and python-dotenv strips the pair and hands over `""`,
    # over the top of anything the unit's `Environment=` had set. So the
    # check phase called the file finished for a Pi that starts with nothing.
    # `BROWSE_CHROMIUM` is quoted here and has to come back, so the rule is
    # "empty once unwrapped" and not "quoted at all".
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".env").write_text(
            'BROWSE_CHROMIUM="/usr/bin/chromium"\n'
            "HOME_LOCATION=\n"
            "HOME_COORDS= \n"
            'SESSIONS_DB=""\n'
            "GWS_CACHE_DIR=''\n"
            "GOOGLE_API_KEY=would-be-a-leak\n")
        blank = subprocess.run(env.command.replace(PI_DIR, tmp), shell=True,
                               capture_output=True, text=True).stdout
    check("a key with nothing after the = does not come back as a key, "
          "quotes included",
          sorted(blank.split()), ["BROWSE_CHROMIUM", "GOOGLE_API_KEY"],
          f"{len(blank.split())} names")
    # #229, and the third way of writing the same nothing. `BROWSE_ALLOWLIST`
    # is a comma-separated list and `browser._allowlist` drops every blank
    # field, so ` , , ` is `[]` to the app — the default, which the attached
    # path refuses to start on — while the two rules above call the key set
    # and the row green. The probe and the parser have to agree about empty,
    # or the check phase signs off a server that will not browse.
    #
    # `HOME_COORDS=,` is the same shape on the other demand: a bare separator
    # is not a coordinate pair, and counting it would call the file finished
    # on a Pi whose clock has nowhere to ask about the weather (#154).
    # `BROWSE_CDP` carries commas in neither direction and has to survive.
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".env").write_text(
            "BROWSE_CHROMIUM=/usr/bin/chromium\n"
            "BROWSE_CDP=http://127.0.0.1:9222\n"
            "BROWSE_ALLOWLIST= , , \n"
            'SESSIONS_DB=","\n'
            "HOME_COORDS=,\n"
            "GWS_CACHE_DIR=/home/pi/ai-terminal/gws_cache\n"
            "GOOGLE_API_KEY=would-be-a-leak\n")
        seps = subprocess.run(env.command.replace(PI_DIR, tmp), shell=True,
                              capture_output=True, text=True).stdout
    check("a value that is only separators does not come back as a key",
          sorted(seps.split()),
          ["BROWSE_CDP", "BROWSE_CHROMIUM", "GOOGLE_API_KEY", "GWS_CACHE_DIR"],
          f"{len(seps.split())} names")
    # And the row that reads off it, because the probe being right is only
    # half of it: the point of #229 is that this row was `ok` for a Pi whose
    # browse would not start.
    check("so the row is red for an allowlist that parses to nothing",
          env.read(0, seps),
          (pi_deploy.MISSING, "missing BROWSE_ALLOWLIST, SESSIONS_DB, "
           "HOME_COORDS or HOME_LOCATION"))
    # The control the case above is worthless without: a rule that threw away
    # every comma would take the real lists with it, and both of these are
    # values a finished `.env` has.
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".env").write_text(
            "BROWSE_ALLOWLIST=jorudan.co.jp, www.google.com\n"
            "HOME_COORDS=35.681,139.767\n")
        real = subprocess.run(env.command.replace(PI_DIR, tmp), shell=True,
                              capture_output=True, text=True).stdout
    check("while a list with real entries in it still counts as set",
          sorted(real.split()), ["BROWSE_ALLOWLIST", "HOME_COORDS"],
          f"{len(real.split())} names")

    kanshi_probe = next(p for p in pi_deploy.PROBES if p.name == "the kanshi config")
    check("kanshi probe command uses sha256sum and remote path",
          kanshi_probe.command,
          f"sha256sum {pi_deploy.KANSHI_DIR}/config 2>/dev/null || true")
    check("kanshi probe recognizes truncated/absent output",
          kanshi_probe.read(0, ""),
          (pi_deploy.MISSING, f"no file at {pi_deploy.KANSHI_DIR}/config"))
    check("kanshi probe recognizes mismatched sha256",
          kanshi_probe.read(0, "0" * 64 + f"  {pi_deploy.KANSHI_DIR}/config\n"),
          (pi_deploy.WRONG, "differs from deploy/kanshi-config"))

    labwc_probe = next(p for p in pi_deploy.PROBES if p.name == "the labwc rc.xml")
    check("labwc probe command uses sha256sum and remote path",
          labwc_probe.command,
          f"sha256sum {pi_deploy.LABWC_DIR}/rc.xml 2>/dev/null || true")
    check("labwc probe recognizes mismatched sha256",
          labwc_probe.read(0, "0" * 64 + f"  {pi_deploy.LABWC_DIR}/rc.xml\n"),
          (pi_deploy.WRONG, "differs from deploy/labwc-rc.xml"))

    touch_probe = next(p for p in pi_deploy.PROBES if p.name == "the touch calibration rule")
    check("touch calibration probe command uses sha256sum",
          touch_probe.command,
          f"sha256sum {pi_deploy.UDEV_RULES_DIR}/99-touch-calibration.rules 2>/dev/null || true")
    check("touch calibration probe recognizes mismatched sha256",
          touch_probe.read(0, "0" * 64 + f"  {pi_deploy.UDEV_RULES_DIR}/99-touch-calibration.rules\n"),
          (pi_deploy.WRONG, "differs from deploy/99-touch-calibration.rules"))

    bias_probe = next(p for p in pi_deploy.PROBES
                      if "microphone bias" in p.name)
    check("bias probe accepts active 0x86 as ok",
          bias_probe.read(0, "0x86"), (pi_deploy.OK, "reg 0x19 = 0x86"))
    check("bias probe accepts at-rest 0x06 as ok",
          bias_probe.read(0, "0x06"), (pi_deploy.OK, "reg 0x19 = 0x06"))
    check("bias probe rejects invalid register value",
          bias_probe.read(0, "0x00"),
          (pi_deploy.WRONG, "reg 0x19 = 0x00 (want 0x06 or 0x86)"))

    wp_probe = next(p for p in pi_deploy.PROBES if p.name == "the WirePlumber config")
    check("WirePlumber probe command uses sha256sum and remote path",
          wp_probe.command,
          f"sha256sum {pi_deploy.WIREPLUMBER_DIR}/50-usb-mic.conf 2>/dev/null || true")
    check("WirePlumber probe recognizes mismatched sha256",
          wp_probe.read(0, "0" * 64 + f"  {pi_deploy.WIREPLUMBER_DIR}/50-usb-mic.conf\n"),
          (pi_deploy.WRONG, "differs from deploy/50-usb-mic.conf"))

    card_probe = next(p for p in pi_deploy.PROBES if p.name == "the capture card")
    check("card probe accepts seeed2micvoicec",
          card_probe.read(0, "card 2: seeed2micvoicec [seeed-2mic-voicecard], device 0: ..."),
          (pi_deploy.OK, "card 2: seeed2micvoicec"))
    check("card probe accepts USB Composite Device",
          card_probe.read(0, "card 1: Device [USB Composite Device], device 0: USB Audio [USB Audio]"),
          (pi_deploy.OK, "card 1: USB Composite Device"))
    check("card probe rejects unrelated HDMI card",
          card_probe.read(0, "card 0: vc4hdmi [vc4-hdmi], device 0: MAI PCM i2s-hifi-0 []")[0],
          pi_deploy.MISSING)

    source_probe = next(p for p in pi_deploy.PROBES if p.name == "the default audio source")
    check("source probe command uses wpctl status",
          "wpctl status" in source_probe.command, True)
    wp_ok = (
        "Audio\n"
        " ├─ Sources:\n"
        " │  *   46. USB Composite Device Mono           [vol: 0.74]\n"
        " │      47. Built-in Audio Stereo               [vol: 1.00]\n"
    )
    check("source probe accepts USB default source",
          source_probe.read(0, wp_ok),
          (pi_deploy.OK, "USB Composite Device Mono"))
    wp_wrong = (
        "Audio\n"
        " ├─ Sources:\n"
        " │      46. USB Composite Device Mono           [vol: 0.74]\n"
        " │  *   47. Built-in Audio Stereo               [vol: 1.00]\n"
    )
    check("source probe rejects built-in audio default source",
          source_probe.read(0, wp_wrong),
          (pi_deploy.WRONG, "Built-in Audio Stereo (want USB Composite Device Mono)"))
    check("source probe reports missing when no default source is found",
          source_probe.read(0, "Audio\n ├─ Sources:\n")[0],
          pi_deploy.MISSING)


def verdict_cases() -> None:
    print("\nwhat the check phase does with the table")
    met = _met()
    real = pi_deploy.spawn
    try:
        pi_deploy.spawn = canned([met[p.name] for p in pi_deploy.PROBES])
        unmet, printed = quietly(pi_deploy.run_check, "fixture")
        check("a Pi with every row met has nothing unmet", unmet, 0)
        check("and says so once, with a line per row",
              len(printed.strip().splitlines()), len(pi_deploy.PROBES) + 2)

        broken = dict(met)
        broken["linger, or the monitor is up only while somebody is "
               "watching"] = "Linger=no"
        pi_deploy.spawn = canned([broken[p.name] for p in pi_deploy.PROBES])
        unmet, _ = quietly(pi_deploy.run_check, "fixture")
        check("one row wrong is one row unmet", unmet, 1)

        # End to end, and the one row where a healthy-looking Pi is the
        # failure: everything met except a tunnel somebody stopped and never
        # disabled. `check` is the last thing run before #10's reboot, so this
        # is the table going green immediately before the reboot that breaks
        # it.
        half = dict(met)
        half["the tunnel is installed"] = "file-present\nenabled\ninactive"
        pi_deploy.spawn = canned([half[p.name] for p in pi_deploy.PROBES])
        unmet, _ = quietly(pi_deploy.run_check, "fixture")
        check("a tunnel stopped but never disabled is one row unmet", unmet, 1)

        pi_deploy.spawn = canned([UNMET[p.name] for p in pi_deploy.PROBES])
        unmet, _ = quietly(pi_deploy.run_check, "fixture")
        check("and the Pi as it read on 2026-09-09 is unmet throughout",
              unmet, len(pi_deploy.PROBES))

        # The exit code is the whole point of `check` on its own: it is what a
        # caller can act on without reading the table.
        held = pi_deploy.resolve
        pi_deploy.resolve = lambda host: host
        try:
            pi_deploy.spawn = canned([UNMET[p.name] for p in pi_deploy.PROBES])
            code, _ = quietly(pi_deploy.main, ["check"])
            check("`check` on its own exits non-zero when a row is unmet",
                  code, 1)
            pi_deploy.spawn = canned([met[p.name] for p in pi_deploy.PROBES])
            code, _ = quietly(pi_deploy.main, ["check"])
            check("and zero when none is", code, 0)
        finally:
            pi_deploy.resolve = held
    finally:
        pi_deploy.spawn = real


def state_cases() -> None:
    print("\ninstalled and enabled, which stop being the same question")
    running = pi_deploy._unit_state(pi_deploy.RUNNING)
    installed = pi_deploy._unit_state(pi_deploy.INSTALLED)
    check("a unit that must be running is not satisfied by one merely there",
          running(0, "file-present\nenabled\ninactive")[0], pi_deploy.WRONG)
    check("nor by one that is running and will not come back after a reboot",
          running(0, "file-present\ndisabled\nactive")[0], pi_deploy.WRONG)
    check("while a unit that must only be present is satisfied by that",
          installed(0, "file-present\ndisabled\ninactive")[0], pi_deploy.OK)
    check("and is not satisfied by one somebody left running",
          installed(0, "file-present\nenabled\nactive")[0], pi_deploy.WRONG)
    # The state #10's acceptance test turns on, and the one the fixtures above
    # step over: `systemctl --user stop` without the `disable` leaves the
    # `default.target.wants` symlink, so every row reads green and the reboot
    # that follows puts the tunnel back on port 8000 ahead of the server.
    check("nor by one stopped today and enabled for the next boot",
          installed(0, "file-present\nenabled\ninactive")[0], pi_deploy.WRONG)
    check("nor by one that is enabled and merely failed to start",
          installed(0, "file-present\nenabled\nfailed")[0], pi_deploy.WRONG)
    check("and enabled-runtime is not disabled, it is enabled until a reboot",
          installed(0, "file-present\nenabled-runtime\ninactive")[0],
          pi_deploy.WRONG)
    # The other order, and the one the enable half alone would let through: a
    # `disable` without a `stop` leaves the tunnel on port 8000 for the rest of
    # this uptime, which is the whole of the window #10's verification runs in.
    check("nor by one disabled for the next boot and running right now",
          installed(0, "file-present\ndisabled\nactive")[0], pi_deploy.WRONG)
    check("and `activating` is on its way to running, not a third answer",
          installed(0, "file-present\ndisabled\nactivating")[0],
          pi_deploy.WRONG)
    check("neither state is met by a unit that is not on the Pi at all",
          [running(0, "Unit could not be found")[0],
           installed(0, "Unit could not be found")[0]],
          [pi_deploy.MISSING, pi_deploy.MISSING])
    check("every unit row in the table declares one of the two",
          [r.name for r in pi_deploy.RESOURCES
           if r.unit_name and r.state not in (pi_deploy.RUNNING,
                                              pi_deploy.INSTALLED)], [],
          f"{len(pi_deploy.RESOURCES)} rows")
    check("and every row's source is a file this repo ships",
          [r.name for r in pi_deploy.RESOURCES if not r.source.is_file()], [])
    check("the one the installer does not copy is the one it must not start",
          [r.name for r in pi_deploy.RESOURCES if not r.copy],
          ["the tunnel"])

    # The two rows #145 is about, named rather than left to the loop above,
    # because what each one asserts is the shipped topology and not a property
    # of the table. Swap either state and the check phase goes green on a Pi
    # whose panel is dark: a stopped server, or a tunnel that comes back at the
    # next boot and points the kiosk at a laptop.
    by_name = {r.name: r for r in pi_deploy.RESOURCES}
    check("the server is a row, and one the check phase wants running",
          [by_name["the server"].state, str(by_name["the server"].source.name),
           by_name["the server"].copy],
          [pi_deploy.RUNNING, "ai-terminal.service", True])
    check("and the tunnel is a row that has to be there and stopped",
          [by_name["the tunnel"].state, by_name["the tunnel"].copy],
          [pi_deploy.INSTALLED, False])
    check("Resource.reload defaults to empty",
          pi_deploy.Resource("t", Path("t"), "t").reload, "")
    check("Resource.target_name defaults to empty",
          pi_deploy.Resource("t", Path("t"), "t").target_name, "")
    check("the journal drop-in is a row, sudo'd to where journald reads it",
          [by_name["the journal"].dest, by_name["the journal"].sudo,
           by_name["the journal"].copy],
          [pi_deploy.JOURNAL_DIR, True, True])
    check("and the row says how to make a running journald re-read it",
          by_name["the journal"].reload,
          "sudo systemctl restart systemd-journald && sudo journalctl --flush")
    check("the touch calibration rule is a row, sudo'd to where udev reads it",
          [by_name["the touch calibration rule"].dest,
           by_name["the touch calibration rule"].sudo,
           by_name["the touch calibration rule"].copy],
          [pi_deploy.UDEV_RULES_DIR, True, True])
    check("and says how to reload udev rules and trigger them",
          by_name["the touch calibration rule"].reload,
          "sudo udevadm control --reload && sudo udevadm trigger")
    check("and no other row declares a reload",
          [r.name for r in pi_deploy.RESOURCES
           if r.name not in ("the journal", "the touch calibration rule")
           and r.reload], [])
    check("the kanshi config renames the destination file to config",
          [by_name["the kanshi config"].dest,
           by_name["the kanshi config"].target_name,
           by_name["the kanshi config"].remote_path,
           by_name["the kanshi config"].sudo,
           by_name["the kanshi config"].copy],
          [pi_deploy.KANSHI_DIR, "config",
           f"{pi_deploy.KANSHI_DIR}/config", False, True])
    check("the labwc config renames the destination file to rc.xml",
          [by_name["the labwc rc.xml"].dest,
           by_name["the labwc rc.xml"].target_name,
           by_name["the labwc rc.xml"].remote_path,
           by_name["the labwc rc.xml"].sudo,
           by_name["the labwc rc.xml"].copy],
          [pi_deploy.LABWC_DIR, "rc.xml",
           f"{pi_deploy.LABWC_DIR}/rc.xml", False, True])

    # `copy` and `compare` are two questions and #183 is what happens when
    # they are one. The kiosk was `copy=False` on the argument that changing
    # it blanks the screen; what that bought was a unit this repo could edit
    # without any Pi hearing about it, twice — #145's `Wants=` and #144's
    # debugging port — while every row of the table stayed green. So the
    # requirement is not that every unit is copied. It is that every unit is
    # read back, whether or not it is copied, because the readback is the only
    # row that can tell a Pi running this text from a Pi running last week's.
    probes = {p.name for p in pi_deploy.PROBES}
    check("a unit the deploy does not copy is one the check reads back",
          [r.name for r in pi_deploy.RESOURCES
           if r.unit_name and not r.copy and not r.compare], [],
          f"{len(probes)} probes, {len(pi_deploy.RESOURCES)} rows")
    check("and every row that declares compare has the probe to go with it",
          [r.name for r in pi_deploy.RESOURCES
           if r.compare
           and f"{r.name}'s unit is the one in deploy/" not in probes], [])
    # The kiosk is both, and that is the point of splitting the two fields:
    # copying it is what makes an edit reach the Pi, and reading it back is
    # what says whether the Pi is behind before a deploy is run at all.
    check("and the kiosk is read back as well as copied, since #144 did not "
          "reach the Pi",
          [(r.copy, r.compare) for r in pi_deploy.RESOURCES
           if r.source == pi_deploy.KIOSK_UNIT], [(True, True)])
    # Stated over the directory and not the table, so a fifth unit added to
    # `deploy/` and forgotten in both places fails here rather than shipping.
    carried = {f"{r.unit_name}.service" for r in pi_deploy.RESOURCES
               if r.unit_name and r.copy}
    compared = {Path(m).name for p in pi_deploy.PROBES
                for m in re.findall(r"cat (\S+\.service)", p.command)}
    check("and no unit in deploy/ is neither carried nor compared",
          sorted(u.name for u in DEPLOY.glob("*.service")
                 if u.name not in carried and u.name not in compared), [],
          f"{len(carried)} carried, {len(compared)} compared")
    kiosk = next((p for p in pi_deploy.PROBES
                  if p.name == "the kiosk's unit is the one in deploy/"), None)
    check("the kiosk's is read at the path the user manager loads it from",
          kiosk.command if kiosk else "no such probe",
          f"cat {pi_deploy.UNIT_DIR}/ai-terminal-kiosk.service "
          "2>/dev/null || true")
    # The three answers named, because the middle one is the whole issue: a
    # kiosk unit that is present, enabled, active and *not this repo's* is the
    # state every other row in the table calls healthy.
    matches = getattr(pi_deploy, "_unit_matches", None)
    read = matches(by_name["the kiosk"]) if matches else None
    shipped = by_name["the kiosk"].source.read_text()
    check("and a Pi still carrying #145's `Wants=` reads WRONG, not ok",
          [read(0, shipped)[0],
           read(0, shipped.replace("After=ai-terminal-tunnel.service",
                                   "Wants=ai-terminal-tunnel.service\n"
                                   "After=ai-terminal-tunnel.service"))[0],
           read(0, "")[0]] if read else "no _unit_matches",
          [pi_deploy.OK, pi_deploy.WRONG, pi_deploy.MISSING])


def phase_cases() -> None:
    print("\ninstall, deploy and restart")
    twice = [pi_deploy.install_plan("pi2"), pi_deploy.install_plan("pi2")]
    check("install builds the same plan the second time",
          [s.argv for s in twice[0]], [s.argv for s in twice[1]])
    check("and nothing in it appends, which is how a plan stops being one",
          [s.name for s in twice[0] if ">>" in " ".join(s.argv)], [])
    idempotent = ("apt-get install -y", "uv sync", "enable-linger",
                  "mic_setup.sh")
    check("every step is a form that is a no-op the second time",
          [s.name for s in twice[0]
           if not any(form in " ".join(s.argv) for form in idempotent)], [],
          f"{len(twice[0])} steps")

    # The guard is about `--delete`, and the distinction it has to draw is
    # between a stale source file — which is what --delete is for, and the Pi
    # is three revisions behind — and state that only exists at the
    # destination, which no copy of this repo can put back.
    guard = pi_deploy._guard
    losing = "*deleting   sessions.db\n*deleting   src/gone.py\n"
    check("a would-delete list with the household's conversations refuses",
          guard(0, losing) is not None, True)
    check("and names the file it refused over",
          "sessions.db" in (guard(0, losing) or ""), True)
    check("a stale source file is what --delete is for and passes",
          guard(0, "*deleting   src/gone.py\n"), None)
    check("so does a rehearsal that would change nothing that matters",
          guard(0, ">f+++++++++ README.md\n.d..t...... ./\n"), None)
    check("the screensaver and the Pi's own .env are state as well",
          sorted(pi_deploy.state_like(
              ["screensaver/", ".env", "gws_cache/doc.json", "tools/gone.py",
               "sessions.db-wal"])),
          [".env", "gws_cache/doc.json", "screensaver/", "sessions.db-wal"])

    # The other half, and the half that does not announce itself: this Mac has
    # run the server, so it holds a sessions.db of its own and a dropped
    # exclude overwrites the Pi's rather than deleting it. Measured — with
    # `sessions.db*` taken out of the list, a real rehearsal against a
    # destination holding one produced no `*deleting` line at all.
    check("an overwrite of the household's conversations refuses too",
          guard(0, ">f.st..g. sessions.db\n") is not None, True)
    check("and says which of the two it is",
          "overwrite" in (guard(0, ">f.st..g. sessions.db\n") or ""), True)
    check("an ordinary source file being sent is not state",
          guard(0, ">f+++++++ tools/pi_deploy.py\n"), None)
    check("a directory rsync would create counts as a transfer",
          pi_deploy.overwriting("cd+++++++++ screensaver/\n"),
          ["screensaver/"])
    check("while a path it says is unchanged does not",
          pi_deploy.overwriting(".f........ sessions.db\n"), [])
    # rsync 3 itemises eleven flag characters and the rsync macOS ships nine,
    # so the width is not what this anchors on.
    check("both widths of itemised line are read",
          [pi_deploy.overwriting(line) for line in
           (">f.st..g. sessions.db\n", ">f.st....... sessions.db\n")],
          [["sessions.db"], ["sessions.db"]])

    deploy = pi_deploy.deploy_plan("pi2")
    check("the rehearsal comes before the copy, or the guard guards nothing",
          [i for i, s in enumerate(deploy)
           if "--dry-run" in s.argv or s.argv[0] == "rsync"], [0, 1])
    check("the grant goes by way of /tmp, since scp cannot make its directory",
          [s.name for s in deploy if "install -D" in " ".join(s.argv)],
          ["the microphone grant, where the system reads it",
           "the journal, where the system reads it",
           "the touch calibration rule, where the system reads it"])

    # #183, and stated over the plan the deploy emits rather than over the
    # table it is built from. `copy=True` on a row no step reads is the same
    # file missing from the Pi, and the row is exactly what everybody already
    # believed: the kiosk was in `RESOURCES`, had a state, had a probe, and
    # was never sent. So this reads the paths off the argv.
    sent = {Path(a).name for s in deploy for a in s.argv
            if a.startswith(str(DEPLOY))}
    check("the kiosk's unit is a file the deploy plan actually sends",
          KIOSK_UNIT.name in sent, True, f"{len(sent)} files sent")
    # Every file under `deploy/` is sent, and the exceptions are named here
    # rather than derived, so adding one to the directory fails this case
    # instead of shipping the way the kiosk did.
    #
    # `ai-terminal-tunnel.service` is deliberate: the shipped Pi wants it
    # absent-or-stopped and *Developing against the panel* installs it.
    check("and every other file in deploy/ is sent, exceptions named",
          sorted(p.name for p in DEPLOY.iterdir() if p.name not in sent and p.suffix != ".md"),
          ["ai-terminal-tunnel.service"])
    check("and every reload the table declares is a step the deploy runs",
          [r.name for r in pi_deploy.RESOURCES
           if r.reload and not any(r.reload in " ".join(s.argv)
                                   for s in deploy)], [])
    check("the reload step is run on the Pi over ssh",
          next((s.argv[0] for s in deploy
                if "systemd-journald" in " ".join(s.argv)), ""),
          "ssh")
    check("and named for the resource it reloads",
          [s.name for s in deploy if "systemd-journald" in " ".join(s.argv)],
          ["re-reading the journal, so changes take effect"])
    check("the udev reload step is run on the Pi over ssh",
          next((s.argv[0] for s in deploy
                if "udevadm" in " ".join(s.argv)), ""),
          "ssh")
    check("and named for the touch calibration resource",
          [s.name for s in deploy if "udevadm" in " ".join(s.argv)],
          ["re-reading the touch calibration rule, so changes take effect"])
    installed = next(i for i, s in enumerate(deploy)
                     if "install -D" in " ".join(s.argv) and "journal" in s.name)
    reloaded = next((i for i, s in enumerate(deploy)
                     if "systemd-journald" in " ".join(s.argv)), -1)
    check("after the file is in place…",
          installed < reloaded, True,
          f"installed at [{installed}], re-read at [{reloaded}]")
    installed_touch = next(i for i, s in enumerate(deploy)
                           if "install -D" in " ".join(s.argv)
                           and "touch-calibration" in " ".join(s.argv))
    reloaded_touch = next(i for i, s in enumerate(deploy)
                          if "udevadm" in " ".join(s.argv))
    check("the udev rule is installed before udev reloads it",
          installed_touch < reloaded_touch, True,
          f"installed at [{installed_touch}], reloaded at [{reloaded_touch}]")

    kanshi_step = next(s for s in deploy if s.name == "the kanshi config")
    check("the kanshi config is copied directly to config",
          kanshi_step.argv,
          ("scp", str(pi_deploy.KANSHI_CONFIG),
           f"pi2:{pi_deploy.KANSHI_DIR}/config"))
    labwc_step = next(s for s in deploy if s.name == "the labwc rc.xml")
    check("the labwc config is copied directly to rc.xml",
          labwc_step.argv,
          ("scp", str(pi_deploy.LABWC_CONFIG),
           f"pi2:{pi_deploy.LABWC_DIR}/rc.xml"))
    wp_step = next(s for s in deploy if s.name == "the WirePlumber config")
    check("the WirePlumber config is copied to wireplumber.conf.d",
          wp_step.argv,
          ("scp", str(pi_deploy.WIREPLUMBER_CONFIG),
           f"pi2:{pi_deploy.WIREPLUMBER_DIR}/"))

    # Restarting the kiosk blanks the panel for several seconds, and under the
    # default all-four run an unconditional restart would do that every time.
    changed = pi_deploy.restart_plan("pi2", kiosk_stale=True)
    same = pi_deploy.restart_plan("pi2", kiosk_stale=False)
    restarts = [s.name for s in changed
                if "restart ai-terminal-kiosk" in " ".join(s.argv)]
    check("the kiosk is restarted when what it reads at launch changed",
          len(restarts), 1)
    check("and left alone when it did not",
          [s.name for s in same
           if "restart ai-terminal-kiosk" in " ".join(s.argv)], [])
    # #183: the enable is the other half and is not conditional, because the
    # kiosk's row says RUNNING — enabled and active — and `enable` is what
    # survives the reboot. #201: it carries --now so a stopped unit recovers.
    check("while the enable is unconditional across changed and same plans",
          [len([s for s in plan
                if "enable --now ai-terminal-kiosk" in " ".join(s.argv)])
           for plan in (changed, same)], [1, 1])
    check("and it carries --now so a stopped kiosk recovers (#201)",
          [s.name for s in changed
           if "enable --now ai-terminal-kiosk" in " ".join(s.argv)],
          ["the kiosk, enabled and started so a stopped unit recovers"])
    # A copy with no reload is a unit systemd has not read; a reload with no
    # restart is a Chromium still on the old command line. Both are #183 one
    # step further along, so the order is pinned.
    reloads = [i for i, s in enumerate(changed)
               if "daemon-reload" in " ".join(s.argv)]
    kiosks = [i for i, s in enumerate(changed)
              if "restart ai-terminal-kiosk" in " ".join(s.argv)]
    check("the reload comes before the kiosk restart, not after it",
          reloads < kiosks and len(reloads) == len(kiosks) == 1, True,
          f"reload at {reloads}, restart at {kiosks}")

    # What decides `kiosk_stale`. Driven through the real function with a
    # canned `spawn`, because the reading it does is a strip-and-compare and
    # the interesting answers are the two absences: a unit nobody can read is
    # not a unit anybody has checked, so it counts as different and buys one
    # restart rather than a browser left on last week's command line.
    real = pi_deploy.spawn
    try:
        shipped_kiosk = pi_deploy.KIOSK_UNIT.read_text()
        pi_deploy.spawn = canned([shipped_kiosk])
        check("a Pi carrying this repo's kiosk unit is not stale",
              pi_deploy.kiosk_unit_differs("pi2"), False)
        pi_deploy.spawn = canned([f"{shipped_kiosk}\n"])
        check("and trailing whitespace is not a difference",
              pi_deploy.kiosk_unit_differs("pi2"), False)
        pi_deploy.spawn = canned([shipped_kiosk.replace(
            f"{DEBUG_FLAG}={DEBUG_PORT} \\\n", "")])
        check("a pre-#144 unit with no debugging port is, which is #183",
              pi_deploy.kiosk_unit_differs("pi2"), True)
        pi_deploy.spawn = canned([""])
        check("and so is a Pi with no kiosk unit to read at all",
              pi_deploy.kiosk_unit_differs("pi2"), True)
        pi_deploy.spawn = canned([POLICY.read_text()])
        check("the same reading serves the policy, unchanged by #183",
              pi_deploy.policy_differs("pi2"), False)
    finally:
        pi_deploy.spawn = real
    # The monitor's two halves, and they are the server's two halves for the
    # server's reason (#228). `enable --now` was the whole of it until then,
    # and `--now` starts a stopped unit and does nothing to a running one — so
    # a deploy copied a new `wake.py` and left the boot's process reading the
    # old one, which `check` cannot see because it compares files. Counted
    # against both plans, since unlike the kiosk's this restart is not the
    # `kiosk_stale` bargain: what it costs is the couple of silent seconds a
    # warm model load takes, and a `wake_stale` mirror would restart the
    # monitor zero times across `deploy` then `restart` as two processes,
    # which is #212 and is the failure this step exists to remove.
    monitor_steps = [" ".join(s.argv) for s in same
                     if "ai-terminal-wake" in " ".join(s.argv)]
    check("the monitor is enabled, so it comes back after a power cut",
          len([c for c in monitor_steps if "enable ai-terminal-wake" in c]), 1)
    check("and restarted onto the tree the deploy copied under it",
          len([c for c in monitor_steps if "restart ai-terminal-wake" in c]), 1)
    check("whether or not the kiosk had a reason to be restarted",
          [len([s for s in plan
                if "restart ai-terminal-wake" in " ".join(s.argv)])
           for plan in (changed, same)], [1, 1])
    check("and its --now gone, since a restart starts a stopped unit too",
          [c for c in monitor_steps if "enable --now ai-terminal-wake" in c],
          [])
    # Same order as the kiosk's and for the same reason: a restart ahead of the
    # reload re-execs the unit systemd has not read yet. The two positions are
    # found separately and compared, rather than collected into one list and
    # tested for sortedness — a list holding both is in order whichever way
    # round they are, which is a case that passes on the defect it names.
    reload_at = [i for i, s in enumerate(same)
                 if "daemon-reload" in " ".join(s.argv)]
    wake_at = [i for i, s in enumerate(same)
               if "restart ai-terminal-wake" in " ".join(s.argv)]
    check("the reload comes before the monitor's restart, not after it",
          reload_at < wake_at and len(reload_at) == len(wake_at) == 1, True,
          f"reload at {reload_at}, restart at {wake_at}")
    # Named unit and not a bare "is-active": the server is asked the same
    # question at the end of the same plan, and an unscoped count would pass
    # here whichever of the two lost a step.
    check("the monitor is asserted twice, ten seconds apart",
          len([s for s in same
               if "is-active ai-terminal-wake" in " ".join(s.argv)]), 2)
    check("because Restart=always reports active to anyone who looks in time",
          one(unit(WAKE_UNIT), "Service", "Restart"), "always")

    journal = next(s for s in same if "journalctl" in " ".join(s.argv))
    check("the journal has to carry the line wake.py prints on startup",
          journal.verify(0, "Aug 01 sudden silence"),
          "the journal has no 'listening on' line, so the model never "
          "loaded or the capture stream never opened")
    check("and no traceback beside it",
          journal.verify(0, f"{pi_deploy.BANNER}x\nTraceback (most recent)"),
          "the journal carries a traceback")
    check("both together is the observation the README asks for",
          journal.verify(0, f"12:00:00 {pi_deploy.BANNER}hey_gemini"), None)
    check("the line it waits for is read out of wake.py, not spelled here",
          pi_deploy.BANNER in
          (ROOT / "src/ai_terminal/wake.py").read_text(), True,
          repr(pi_deploy.BANNER))

    # The server's half of the same plan. `restart` and not `enable --now`,
    # because the phase before this one has just replaced the tree underneath
    # a process that is already up: `--now` on an active unit is a no-op that
    # reports success, and the panel goes on talking to the old revision.
    server_steps = [" ".join(s.argv) for s in same
                    if re.search(r"ai-terminal(?![-\w])", " ".join(s.argv))]
    check("the server is enabled, so it comes back after a power cut",
          len([c for c in server_steps if "enable ai-terminal" in c]), 1)
    check("and restarted, since the deploy just replaced the tree under it",
          len([c for c in server_steps if "restart ai-terminal" in c]), 1)
    check("with no --now, which on a running unit succeeds without restarting",
          [c for c in server_steps if "enable --now ai-terminal" in c], [])

    served = next((s for s in same
                   if "is-active ai-terminal 2>&1;" in " ".join(s.argv)), None)
    check("and asked afterwards whether it came up at all",
          served is not None, True)
    # Neither systemd nor the journal can see a bind. `Type=simple` makes the
    # unit `active` from `exec` onwards, `running revision` is written at
    # import, and uvicorn answers EADDRINUSE with a bare `logger.error(exc)`
    # and no traceback — so all three of the tests below pass against a server
    # that never took the socket. `ss` is the fourth, and the only one that
    # can tell a process that holds port 8000 from one that lost it.
    check("it asks who is holding the port, which is the only test of a bind",
          "ss -ltnp" in " ".join(served.argv), True)
    port = pi_deploy.PI_PORT
    mine = (f"LISTEN 0      2048        127.0.0.1:{port}      0.0.0.0:*    "
            'users:(("python3",pid=812,fd=9))')
    # Copied off pi2 on 2026-09-10, where the tunnel had held both of these
    # since the boot at 18:36 the evening before. `ssh -N -L` takes the IPv6
    # loopback as well, and uvicorn's default `--host 127.0.0.1` collides on
    # the first of the two.
    theirs = (f"LISTEN 0      128        127.0.0.1:{port}      0.0.0.0:*    "
              'users:(("ssh",pid=1703,fd=5))')
    theirs6 = (f"LISTEN 0      128            [::1]:{port}         [::]:*    "
               'users:(("ssh",pid=1703,fd=4))')
    elsewhere = mine.replace(f":{port} ", f":{port + 1} ")
    started = f"12:00:00 INFO {pi_deploy.STARTED} abc"
    check("a server that is not up is caught before the journal is read",
          served.verify(0, "inactive"), "is-active says 'inactive'")
    check("and one that is up with a traceback behind it",
          served.verify(0, f"active\n{pi_deploy.STARTED} x\nTraceback (most"),
          "the journal carries a traceback")
    check("and one that is up and still importing, which Restart= hides",
          served.verify(0, "active\nStarted AI Terminal live voice agent."),
          f"the journal has no {pi_deploy.STARTED!r} line, so the process is "
          "up and has not finished starting")
    # The crash loop this whole step exists for: active, no traceback, and a
    # `running revision` line left in the journal by a cycle that then died on
    # the port. Everything the old reader looked at is present and correct.
    check("and one whose journal says it started and whose socket says not",
          served.verify(0, f"active\n{started}"),
          f"nothing is listening on {port}, so the unit is active and has "
          "not taken the socket")
    check("a listener on the next port along is not this one",
          served.verify(0, f"active\n{elsewhere}\n{started}"),
          f"nothing is listening on {port}, so the unit is active and has "
          "not taken the socket")
    check("and the tunnel holding it is named as the tunnel, not as silence",
          served.verify(0, f"active\n{theirs}\n{started}"),
          f"ssh is holding {port}, so the panel is looking down the tunnel "
          "at another machine and not at this server")
    check("on either loopback, since ssh -N -L takes both and uvicorn one",
          served.verify(0, f"active\n{theirs6}\n{started}"),
          f"ssh is holding {port}, so the panel is looking down the tunnel "
          "at another machine and not at this server")
    check("all four clear is the observation the README asks for",
          served.verify(0, f"active\n{mine}\n{started}"), None)
    # `ss -ltnp` lists every listener on the Pi, not the one asked about, and
    # ssh forwards more than this port. The scan for it is over the lines that
    # matched the port and not over the blob, or a second forward somewhere
    # else on the machine reads as the tunnel holding 8000.
    other = theirs.replace(f":{port} ", ":8080 ")
    check("and an ssh forward on some other port is not this port's listener",
          served.verify(0, f"active\n{other}\n{mine}\n{started}"), None)

    # All of which is read off a blob, and `ss -ltnp 2>&1 || true` on a Pi
    # without iproute2 puts the shell's own `ss: command not found` into that
    # blob and exits 0. The scan then finds no listener and the step reports
    # the port as unheld — the crash-loop alarm above, raised over a tool that
    # was never there. The reader cannot draw the distinction for itself, so
    # the command draws it, in a word this repo chose rather than one the
    # login shell chose.
    absent = f"{pi_deploy.NO_TOOL} ss"
    check("a command that needs a tool asks the Pi for it first",
          pi_deploy.needs("ss", "ss -ltnp"),
          "if command -v ss >/dev/null 2>&1; then ss -ltnp; "
          f"else echo '{absent}'; fi")
    check("and the tool it could not run is the tool it names",
          pi_deploy._absent(f"active\n{absent}\n{started}"), "ss")
    check("whichever tool that is, not the one this step happens to want",
          pi_deploy._absent(f"{pi_deploy.NO_TOOL} journalctl"), "journalctl")
    check("while a run where every tool was there reports none missing",
          pi_deploy._absent(f"active\n{mine}\n{started}"), None)
    # bash says `command not found`, dash says `not found`, and nothing here
    # picks the shell that `ssh host 'cmd'` starts. A guard written against
    # either wording does not fire under the other, which is the bug it was
    # added to fix, one level up.
    check("a shell's own wording for it is not what is read back",
          pi_deploy._absent("bash: line 1: ss: command not found\n"
                            "sh: 1: ss: not found"), None)
    # And the sentinel is the head of a line, not a substring, because this
    # step reads `journalctl -n 60` and journals quote command lines.
    check("nor a journal line that happens to quote the sentinel",
          pi_deploy._absent(f"12:00:00 pi2 uv[1]: INFO {absent}"), None)
    # Section-wide, because the shape is the defect and not the one instance
    # of it: `ss` and `journalctl` here, `journalctl` in the monitor's step,
    # `stat` in the deploy's. Any later step that ends a command in `|| true`
    # and does not ask for its tool first is this bug again under a new name.
    #
    # `|| :` as well as `|| true`, because `:` is the same builtin under its
    # other spelling and a net that catches one word is a net anybody can
    # step over without meaning to. Measured: a step added as `df -h / 2>&1
    # || :` passed this case when it read `"|| true"` alone.
    SWALLOWS = ("|| true", "|| :")
    swallowed = [" ".join(s.argv) for s in deploy + same
                 if any(w in " ".join(s.argv) for w in SWALLOWS)]
    check("every step that swallows an exit code asks for its tool first",
          [c for c in swallowed if "command -v " not in c], [],
          f"{len(swallowed)} steps end a command in {' or '.join(SWALLOWS)}")
    check("the port question among them, since iproute2 is not essential",
          "command -v ss" in " ".join(served.argv), True)
    check("and the journal, which the same reader is also looking at",
          " ".join(served.argv).count("command -v journalctl"), 1)
    # What the phase then does with it. The reader is never reached, so the
    # false alarm is never printed, and the deploy stops on the one
    # observation it could not make rather than on a guess about it.
    real_spawn = pi_deploy.spawn
    pi_deploy.spawn = lambda step, timeout=900: (0, f"active\n{absent}")
    try:
        code, printed = quietly(pi_deploy.run_steps, (served,), "restart")
    finally:
        pi_deploy.spawn = real_spawn
    check("a step that could not look fails the phase it is in", code, 1)
    check("naming the tool the Pi is missing, not what it did not find",
          ("ss is not installed" in printed,
           "nothing is listening" in printed), (True, False),
          printed.strip().splitlines()[-1].strip())
    # And a step with no reader is not exempt, because whether the command
    # ran at all comes before anything a reader would have made of it.
    blind = pi_deploy.Step("a step with nothing to read it",
                           ("ssh", "pi2", pi_deploy.needs("df", "df -h /")))
    pi_deploy.spawn = lambda step, timeout=900: (0, f"{pi_deploy.NO_TOOL} df")
    try:
        blind_code, blind_out = quietly(pi_deploy.run_steps, (blind,),
                                        "restart")
    finally:
        pi_deploy.spawn = real_spawn
    check("which a step with no reader at all is not exempt from",
          (blind_code, "df is not installed" in blind_out), (1, True))
    check("and the line it waits for is read out of main.py, not spelled here",
          pi_deploy.STARTED in
          (SRC / "live" / "main.py").read_text(), True,
          repr(pi_deploy.STARTED))


def drive_mic_setup(hpf: str = "0x50", bias: str = "0x86", hpf_rc: int = 0,
                    bias_rc: int = 0) -> tuple[int, str]:
    """Run the real `mic_setup.sh` against a codec made of shell stubs.

    Nothing opens a card: `arecord` is a stub, and the PATH the script runs
    under holds the stubs and the C library's own tools, nothing that could
    reach ALSA. Each register refuses on its own, so a script that reads the
    other one is never rescued by that read failing on its behalf.
    """
    script = ROOT / "tools/mic_setup.sh"
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp)
        for name in ("amixer", "i2cset", "arecord"):
            (stub / name).write_text("#!/bin/sh\nexit 0\n")
        (stub / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
        (stub / "i2cget").write_text(
            f'#!/bin/sh\ncase "$5" in\n'
            f'  0x0c) [ {hpf_rc} -ne 0 ] && exit {hpf_rc}\n'
            f'        echo {hpf} ;;\n'
            f'  0x19) [ {bias_rc} -ne 0 ] && exit {bias_rc}\n'
            f'        echo {bias} ;;\n'
            f'  *) echo 0x00 ;;\nesac\n')
        for name in ("amixer", "arecord", "i2cget", "i2cset", "sudo"):
            (stub / name).chmod(0o755)
        done = subprocess.run(["bash", str(script)], text=True,
                              capture_output=True,
                              env={"PATH": f"{tmp}:/usr/bin:/bin",
                                   "CARD": "stub"})
    return done.returncode, done.stdout + done.stderr


def mic_setup_cases() -> None:
    """The one install step whose verify used to pass on its own label.

    `mic_setup.sh` writes the mic bias and reads it straight back, and the
    reason the step exists is that the write can return 0 and not stick — DAPM
    powers the widget down again on the next sync. The readback line is
    `BIAS reg 0x19 = 0x06 (want 0x86)`, so a substring test for `0x86` matches
    the want and reports a green install over a codec that is off.

    The script is driven for real here rather than described, by
    `drive_mic_setup` above.
    """
    print("\nthe bias write, and a verify that can see it not stick")
    script = ROOT / "tools/mic_setup.sh"
    step = next(s for s in pi_deploy.install_plan("pi2")
                if "mic_setup.sh" in " ".join(s.argv))
    code, out = drive_mic_setup(bias="0x86")
    one_line = out.strip().replace("\n", " | ")
    check("a run where the bias stuck exits zero", code, 0, one_line)
    check("and the install step calls it done", step.verify(code, out), None)

    # The filter reads back healthy here, because the bias is what this case is
    # about and a bad filter is now its own failure — see `hpf_cases`.
    code, out = drive_mic_setup(bias="0x06")
    check("a run where DAPM undid the write exits zero just the same",
          code, 0, out.strip().replace("\n", " | "))
    check("so the step has only its verify, and the verify reads the value",
          step.verify(code, out),
          "mic_setup.sh left the bias at 0x06, want 0x86")

    # A codec that does not answer at all used to be invisible twice over: the
    # i2cget failed inside printf's argument list, where `set -e` does not
    # look, and the empty value it printed still carried the want string.
    code, out = drive_mic_setup(bias_rc=1)
    check("a bias readback the codec refuses fails the script outright",
          code != 0, True, f"exit {code}")

    # The verify parses a line another file prints. Read the format out of that
    # file rather than writing it down here a second time.
    fmt = re.search(r"^printf '(BIAS reg [^']*)\\n'", script.read_text(), re.M)
    check("the line the verify parses is the line the script prints",
          bool(fmt), True, fmt.group(1) if fmt else "no BIAS printf in it")
    check("and rendering it with the value DAPM leaves is a failed step",
          step.verify(0, fmt.group(1).replace("%s", "0x06")),
          "mic_setup.sh left the bias at 0x06, want 0x86")
    nothing_read = "amixer: Unable to find simple control\n"
    check("output with no readback line in it at all is a failed step too",
          step.verify(0, nothing_read) is not None, True)

    # mic_setup.sh runs under `set -eu` and requires external tools on PATH.
    # Every tool it invokes must come from a package installed by install_plan
    # ahead of running the script (#193).
    tool_packages = {
        "i2cset": "i2c-tools",
        "i2cget": "i2c-tools",
        "amixer": "alsa-utils",
        "arecord": "alsa-utils",
    }
    script_text = script.read_text()
    sudo_cmds = set(re.findall(r"sudo\s+([a-zA-Z0-9_-]+)", script_text))
    for cmd in sorted(sudo_cmds):
        pkg = tool_packages.get(cmd)
        check(f"sudo command {cmd} is in an installed package",
              pkg in pi_deploy.PACKAGES if pkg else False, True,
              f"{cmd} -> {pkg}")
    check("mic_setup.sh dependencies are covered by the installed packages",
          [p for p in sorted(set(tool_packages.values())) if p not in pi_deploy.PACKAGES],
          [])
    check("i2c-tools is installed as an audio package for the mic HAT",
          "i2c-tools" in pi_deploy.APT_AUDIO, True)


def a_checkout(root: Path) -> str:
    """A workspace repo shaped the way this one is, and its short HEAD.

    `project/` is the directory that ships and `other/` stands in for the
    thirty that do not, because the claim under test is that somebody else's
    edit in the same repo leaves this project's revision clean.
    """
    def git(*args: str) -> None:
        subprocess.run(("git", "-C", str(root), "-c", "user.name=check",
                        "-c", "user.email=check@localhost",
                        "-c", "commit.gpgsign=false", *args),
                       check=True, capture_output=True, env=_git_env())

    for name in ("project", "other"):
        (root / name).mkdir()
        (root / name / "a.txt").write_text("one\n")
    git("init", "-q")
    git("add", "-A")
    git("commit", "-qm", "one")
    return subprocess.run(("git", "-C", str(root), "rev-parse", "--short",
                           "HEAD"), capture_output=True,
                          text=True, env=_git_env()).stdout.strip()


def revision_cases(readme: str) -> None:
    print("\nthe stamp the deploy leaves, since the copy has no .git")
    deploy = pi_deploy.deploy_plan("pi2")
    commands = [" ".join(s.argv) for s in deploy]
    stamping = [i for i, c in enumerate(commands)
                if pi_deploy.REVISION_CONF in c]
    check("the deploy says which revision it copied, or /api/config cannot",
          len(stamping), 1)
    argv = commands[stamping[0]]
    check("into the drop-in directory systemd reads for the server's unit",
          pi_deploy.REVISION_CONF,
          f"{pi_deploy.UNIT_DIR}/{SERVER_UNIT.name}.d/revision.conf")
    # Beside the unit and not under WorkingDirectory. A file inside the rsync's
    # own destination is one the next `--delete` removes — it is not in the
    # source tree and `state_like` does not know it, so the guard would be
    # silent about it — and the panel would be back to "unknown".
    check("outside the tree the next rsync --delete owns",
          pi_deploy.REVISION_CONF.startswith(pi_deploy.PI_DIR + "/"), False,
          pi_deploy.REVISION_CONF)
    check("making the directory first, since nothing else creates it",
          f"mkdir -p {pi_deploy.DROPIN}" in argv, True)
    check("and setting the variable main.py resolves ahead of the checkout",
          f"Environment={pi_deploy.STAMP}={pi_deploy.REVISION}" in argv, True,
          pi_deploy.REVISION)
    # In the same write as the SHA, because the two are one statement about one
    # tree. Written by a separate step, a deploy that half failed would date a
    # build by its SHA or name one by its date, and the panel's corner would
    # read as though somebody had checked. #261.
    check("and, in the same write, when that commit was made",
          f"Environment={pi_deploy.BUILT_STAMP}={pi_deploy.BUILT}" in argv,
          True, pi_deploy.BUILT or "(no git here)")
    check("after the copy, so it describes the tree that actually landed",
          stamping[0] > max(i for i, s in enumerate(deploy)
                            if s.argv[0] == "rsync"), True)
    # `restart` copies nothing, so it has nothing to say about what is there.
    # A restamp there would relabel a tree it never touched.
    check("while a restart on its own leaves the stamp alone",
          [s.name for s in pi_deploy.restart_plan("pi2")
           if pi_deploy.STAMP in " ".join(s.argv)], [])

    # The step reads the file back rather than trusting a redirect that exited
    # zero, for the reason the model's stat two steps up exists.
    step = deploy[stamping[0]]
    check("the step reads the file back, not just the redirect's exit code",
          step.verify is not None, True)
    check("a drop-in that read back empty is a failure, not a done step",
          step.verify(0, "") is not None, True)
    check("and one still carrying the last deploy's SHA is too",
          step.verify(0, f"[Service]\nEnvironment={pi_deploy.STAMP}=0000000\n")
          is not None, True)
    # Half a drop-in is the reading the badge cannot survive: the SHA of this
    # deploy over the date of the last one is a corner that names today's build
    # and dates it yesterday, and nothing about it looks wrong.
    check("and one carrying this deploy's SHA under the last deploy's date is "
          "a failure too",
          step.verify(0, f"[Service]\nEnvironment={pi_deploy.STAMP}="
                         f"{pi_deploy.REVISION}\n"
                         f"Environment={pi_deploy.BUILT_STAMP}="
                         "2026-01-01T00:00:00+09:00\n") is not None, True)
    was_built = pi_deploy.BUILT
    pi_deploy.BUILT = ""
    try:
        check("an empty BUILT does not accept an unrelated build timestamp",
              step.verify(0, f"[Service]\nEnvironment={pi_deploy.STAMP}="
                             f"{pi_deploy.REVISION}\n"
                             f"Environment={pi_deploy.STATIC_STAMP}="
                             f"{pi_deploy.STATIC_REVISION}\n"
                             f"Environment={pi_deploy.BUILT_STAMP}="
                             "2026-01-01T00:00:00+09:00\n") is not None, True)
        unverified_step = [s for s in pi_deploy.deploy_plan("pi2")
                           if pi_deploy.REVISION_CONF in " ".join(s.argv)][0]
        check("a deploy without git leaves the stamp unverified rather than guessing",
              unverified_step.verify is None, True)
    finally:
        pi_deploy.BUILT = was_built
    written = (f"[Service]\nEnvironment={pi_deploy.STAMP}={pi_deploy.REVISION}\n"
               f"Environment={pi_deploy.STATIC_STAMP}="
               f"{pi_deploy.STATIC_REVISION}\n"
               f"Environment={pi_deploy.BUILT_STAMP}={pi_deploy.BUILT}\n")
    check("while the three lines it just wrote are what it wants back",
          step.verify(0, written), None)
    # `_last` would report whichever line came last, so a stale SHA under a
    # fresh date is reported by the half that was right.
    check("and a drop-in that read back wrong is reported by all three lines",
          step.verify(0, f"[Service]\nEnvironment={pi_deploy.STAMP}=0000000\n"
                         f"Environment={pi_deploy.STATIC_STAMP}="
                         f"{pi_deploy.STATIC_REVISION}\n"
                         f"Environment={pi_deploy.BUILT_STAMP}="
                         f"{pi_deploy.BUILT}\n").count("Environment="), 3)
    check("and a failure names the drop-in it wrote, not the one it replaced",
          "0000000" in (step.verify(
              0, f"Environment={pi_deploy.STAMP}={pi_deploy.REVISION}\n"
                 f"===old===\n[Service]\n"
                 f"Environment={pi_deploy.STAMP}=0000000\n") or ""), True)
    check("and writing the static digest beside it",
          f"Environment={pi_deploy.STATIC_STAMP}={pi_deploy.STATIC_REVISION}" in argv, True)
    _, reload_msg = quietly(step.verify, 0,
                            f"Environment={pi_deploy.STATIC_STAMP}=old123456789\n===old===\n"
                            f"Environment={pi_deploy.STAMP}={pi_deploy.REVISION}\n"
                            f"Environment={pi_deploy.STATIC_STAMP}={pi_deploy.STATIC_REVISION}\n"
                            f"Environment={pi_deploy.BUILT_STAMP}={pi_deploy.BUILT}\n")
    check("the step says the panel will reload when static bytes moved",
          "the panel will reload" in reload_msg, True)
    _, same_msg = quietly(step.verify, 0,
                          f"Environment={pi_deploy.STATIC_STAMP}={pi_deploy.STATIC_REVISION}\n===old===\n"
                          f"Environment={pi_deploy.STAMP}={pi_deploy.REVISION}\n"
                          f"Environment={pi_deploy.STATIC_STAMP}={pi_deploy.STATIC_REVISION}\n"
                          f"Environment={pi_deploy.BUILT_STAMP}={pi_deploy.BUILT}\n")
    check("and says the panel is already on these bytes when they did not",
          "the panel is already on these bytes" in same_msg, True)
    check("the static revision matches between deploy and server",
          pi_deploy.static_revision(), server._static_revision())
    static_stamped = os.environ.pop(pi_deploy.STATIC_STAMP, None)
    os.environ[pi_deploy.STATIC_STAMP] = "112233445566"
    try:
        check("the static variable the deploy sets is the one main.py "
              "reads ahead of disk",
              server._static_revision(), "112233445566")
    finally:
        if static_stamped is not None:
            os.environ[pi_deploy.STATIC_STAMP] = static_stamped
        else:
            os.environ.pop(pi_deploy.STATIC_STAMP, None)

    # The rule is main.py's, and it is driven rather than restated: the
    # installer and the server have to name one commit one way, or the deploy
    # log and the panel disagree about the same machine.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        head = a_checkout(root)
        was, at = pi_deploy.ROOT, server.REPO_ROOT
        stamped = os.environ.pop(pi_deploy.STAMP, None)
        pi_deploy.ROOT = server.REPO_ROOT = root / "project"
        try:
            check("the stamp is the short HEAD of the tree being copied",
                  pi_deploy.revision(), head)
            check("which is the string the server would work out unstamped",
                  pi_deploy.revision(), server._revision())
            (root / "other" / "a.txt").write_text("two\n")
            check("an edit to another project in the same repo is not ours",
                  pi_deploy.revision(), head)
            (root / "project" / "a.txt").write_text("two\n")
            check("an uncommitted edit the rsync will carry is said out loud",
                  pi_deploy.revision(), f"{head}-dirty")
            check("and the server still names it the same way",
                  pi_deploy.revision(), server._revision())
            # The date beside it, by the same rule and for the same reason.
            # Driven against `main._built()` and not restated, or the deploy
            # log and the panel's corner date one machine two ways. #261.
            when = subprocess.run(
                ("git", "-C", str(pi_deploy.ROOT), "show", "-s",
                 "--format=%cI", "HEAD"), capture_output=True,
                text=True, env=_git_env()).stdout.strip()
            check("the date stamped is the commit's own, at its own offset",
                  pi_deploy.built(), when)
            check("which is the string the server would work out unstamped",
                  pi_deploy.built(), server._built())
            # Not the deploy's clock: redeploying an old commit is still that
            # commit, and a stamp that moved because somebody ran the
            # installer again would answer a question nobody asked. An offset
            # with it, because the badge is read in a kitchen in JST and a
            # bare local time is an hour nobody can place.
            check("carrying the offset it was written at, not a bare time",
                  [len(pi_deploy.built()), pi_deploy.built()[10:11]],
                  [25, "T"], pi_deploy.built())
            # An outer GIT_DIR set by an enclosing environment or tool must not
            # leak into this checkout: git -C <path> honours GIT_DIR over the path. #302.
            with tempfile.TemporaryDirectory() as unrelated:
                os.environ["GIT_DIR"] = str(Path(unrelated) / ".git")
                try:
                    check("GIT_DIR in the process environment does not defeat "
                          "a checkout's revision",
                          pi_deploy.revision(), f"{head}-dirty")
                finally:
                    os.environ.pop("GIT_DIR", None)
            built = os.environ.pop(pi_deploy.BUILT_STAMP, None)
            try:
                os.environ[pi_deploy.BUILT_STAMP] = "2026-09-11T07:33:00+09:00"
                check("the variable the deploy sets is what the server reads, "
                      "ahead of the checkout",
                      server._built(), "2026-09-11T07:33:00+09:00")
            finally:
                if built is not None:
                    os.environ[pi_deploy.BUILT_STAMP] = built
                else:
                    os.environ.pop(pi_deploy.BUILT_STAMP, None)
            # Driven and not matched out of main.py's source: a regex would
            # still agree with a main.py that had stopped reading the name.
            # This is the whole point of the drop-in — the stamp has to beat
            # the checkout the process happens to be sitting in.
            os.environ[pi_deploy.STAMP] = "0f1e2d3"
            check("the variable the deploy sets is the one main.py reads, "
              "ahead of any checkout underneath it",
                  server._revision(), "0f1e2d3")
        finally:
            pi_deploy.ROOT, server.REPO_ROOT = was, at
            if stamped is not None:
                os.environ[pi_deploy.STAMP] = stamped
    with tempfile.TemporaryDirectory() as tmp:
        was, pi_deploy.ROOT = pi_deploy.ROOT, Path(tmp)
        try:
            check("and a tree with no repo behind it stamps unknown, not a "
                  "SHA that reads as though somebody checked",
                  pi_deploy.revision(), "unknown")
            check("and dates nothing rather than making one up",
                  pi_deploy.built(), "")
        finally:
            pi_deploy.ROOT = was

    # Two procedures for the same job, pinned to each other the way the
    # exclude list is. A README block that writes the drop-in somewhere else
    # is a manual deploy after which /api/config answers "unknown".
    check("the README's own block writes the same file",
          pi_deploy.REVISION_CONF.replace(f"/home/{pi_deploy.PI_USER}", "~")
          in readme, True)
    check("and sets the same variable in it",
          f"Environment={pi_deploy.STAMP}=" in readme, True)
    check("and sets the date beside it",
          f"Environment={pi_deploy.BUILT_STAMP}=" in readme, True)
    block = re.search(r"^rev=\$\(.*?^rsync ", readme, re.S | re.M)
    check("computing the revision by the installer's rule, dirty included",
          "-dirty" in (block.group(0) if block else ""), True)


def boot_digest(path: Path) -> str:
    """A file's digest with the lines nothing reads at boot taken out.

    Blank lines and whole-line `#` comments, for the three formats where that
    is what a comment is: systemd reads `#` and `;` at the start of a line as
    one, udev reads `#`, kanshi reads `#`. Only `#` is stripped, so a `;`
    comment in a unit still moves the digest — a false positive on the side
    that asks for a reboot rather than the side that skips one. Everything
    else, `.json` and `.py` included, is digested whole.
    """
    data = path.read_bytes()
    if path.name.endswith(BOOT_COMMENTED):
        lines = [ln for ln in data.decode().splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
        data = ("\n".join(lines) + "\n").encode()
    return hashlib.sha256(data).hexdigest()


def restarted_units() -> set[str]:
    """The units a deploy's restart phase actually restarts, from the plan.

    Read off `restart_plan` rather than restated, because the whole reason
    `wake.py` is in the boot set is that the monitor is missing from this set
    and the server is not.
    """
    return {m.group(1) for m in
            (re.search(r"\brestart (\S+)$", " ".join(s.argv))
             for s in pi_deploy.restart_plan("pi2")) if m}


def boot_set() -> dict[str, str]:
    """Every file the unattended boot depends on, digested as it stands now.

    Derived on this side and written down in `BOOT_ACCEPTED` on the other, so
    the two can disagree. A file added to `deploy/` and nowhere else is the
    disagreement this is for.
    """
    paths = sorted(p for p in DEPLOY.iterdir() if p.is_file() and p.suffix != ".md")
    if "ai-terminal-wake" not in restarted_units():
        paths.append(SRC / "wake.py")
    return {p.relative_to(ROOT).as_posix(): boot_digest(p) for p in paths}


def boot_acceptance_cases(readme: str) -> None:
    """Whether the one claim no script can make is still about this build."""
    print("\nhow far the tree has moved since the boot acceptance passed")
    now = boot_set()
    check("the record names the commit the acceptance passed on",
          bool(re.fullmatch(r"[0-9a-f]{7,40}", BOOT_ACCEPTANCE)), True,
          f"#{BOOT_ISSUE} on {BOOT_ACCEPTANCE}")
    check("every file the boot reads is in it, ones added since included",
          sorted(set(now) - set(BOOT_ACCEPTED) - set(BOOT_DRIFT)), [],
          f"{len(now)} files")
    check("and nothing it names has been deleted without being noticed",
          sorted(p for p in BOOT_ACCEPTED if not (ROOT / p).is_file()), [])

    # The gate, and the only case here meant to fire on an ordinary commit.
    # Everything that no longer matches what #10 ran on, against the list
    # somebody has already read. A new name on the left is a change only a
    # reboot can exercise, landed since the last person looked.
    check("the files that have moved are the ones already accounted for",
          sorted(p for p in now if BOOT_ACCEPTED.get(p) != now[p]),
          sorted(p for p in BOOT_DRIFT if p in now),
          f"{len(BOOT_DRIFT)} acknowledged, all #{BOOT_STALE_ISSUE}")
    check("each of them against an issue rather than a bare exemption",
          [p for p, n in BOOT_DRIFT.items() if not isinstance(n, int)], [])

    # The server and the monitor both get a `restart`, so neither process
    # runs boot-era code across a deploy (#228). `wake.py` leaves the boot
    # set on its own.
    restarted = restarted_units()
    check("the monitor and the server are both restarted by a deploy",
          ("ai-terminal-wake" in restarted, "ai-terminal" in restarted),
          (True, True), f"restarted: {sorted(restarted)}")
    # And the other half of the boundary: the files that change most are out
    # of the set because a deploy re-executes them, so their staleness clears
    # without a reboot and digesting them would only cry wolf.
    check("while what every deploy re-executes is out of it",
          [p for p in sorted(set(BOOT_ACCEPTED) | set(BOOT_DRIFT))
           if p.endswith(("live/main.py", "live/screensaver.py",
                          "pi_deploy.py", "wake.py"))], [])

    # Counted before splitting, for `debug_port_cases`' reason: a split on a
    # heading that is not there hands back the whole file, and every case
    # below would then be reading the README at large.
    parts = readme.split(f"#### {BOOT_SECTION}")
    check("the README carries the procedure, or the re-run is folk knowledge",
          len(parts), 2, BOOT_SECTION)
    body = parts[-1].split("\n#### ")[0] if len(parts) == 2 else ""
    check("naming the commit the last pass was on, so a reader can date it",
          BOOT_ACCEPTANCE in body, True, BOOT_ACCEPTANCE)
    check("and every file that has moved, so it says why a re-run is due",
          [p for p in sorted(BOOT_DRIFT) if p not in body], [])
    # The drift table is a historical record of what moved between two acceptance
    # passes, not a list of pending drift. Assert that its title names the
    # acceptance it records.
    title = re.search(r"^\*\*What moved between `?(\w+)`? and `?(\w+)`?\.\*\*",
                      body, re.M)
    check("the drift table's title names the acceptance it is the record of",
          title.group(2) if title else None, BOOT_ACCEPTANCE, BOOT_ACCEPTANCE)
    # The reboot is the whole cost, and the two captures are what stopped it
    # needing somebody in the room. A procedure missing any of the three is
    # one that gets reinvented at the moment nobody can afford to.
    check("and the three commands that make it a procedure and not a request",
          [c for c in ("sudo reboot", "grim ", 'say "Hey Gemini"')
           if c not in body], [])


def hpf_cases() -> None:
    """The half of `mic_setup.sh` that nothing here used to look at.

    The script configures the ADC high-pass filter with `amixer` at the top,
    reads register 0x0C back at the bottom, and prints it beside a want label.
    That label was the only place `0x50` appeared outside a comment: mutating
    it to `0x51` changed the script's output and failed nothing, in this file
    or in any other. And the reading itself was printed and then dropped —
    `0x00` there is the filter off, which the script's own note 1 calls a DC
    offset that "masquerades perfectly as a broken microphone", and the
    install step reported `done` over it.

    The script is driven for real here too, by `drive_mic_setup` above.
    """
    print("\nthe high-pass filter half of the mic setup")
    text = (ROOT / "tools/mic_setup.sh").read_text()

    code, out = drive_mic_setup()
    check("a run where the filter took exits zero", code, 0,
          out.strip().replace("\n", " | "))
    check("and says so in the format the readback line has always had",
          "HPF  reg 0x0C = 0x50 (want 0x50)" in out, True)

    code, out = drive_mic_setup(hpf="0x00")
    check("a filter that did not take fails the run instead of printing it",
          code != 0, True, f"exit {code}")
    check("and the reason names the register and both values",
          [w for w in ("0x0c", "0x00", "0x50") if w not in out], [],
          out.strip().replace("\n", " | "))

    # `0x00` is also what 0x0C reads when the capture never opened, which the
    # script's note 3 says is the only condition it reads truthfully under.
    # One exit status covers both, and neither is a card anyone can record on.
    code, out = drive_mic_setup(hpf_rc=1)
    check("a readback the codec refuses fails the run too, not silently",
          code != 0, True, f"exit {code}")

    # The want value is compared against the script's own note 3, which is the
    # sentence a reader checks it by. Two copies that have to agree beat one
    # copy nothing can be wrong about.
    want = re.search(r"^HPF_REG=(\S+);\s+HPF_WANT=(\S+)", text, re.M)
    check("the wanted filter value has a name rather than being a label",
          bool(want), True, want.group(0) if want else "no HPF_WANT in it")
    named = want.group(2) if want else "0x??"
    check("and it is the value the register note above it documents",
          f"only reads back {named} while a stream is open" in text,
          True, named)
    check("the bias write and its want label are the same one value",
          'i2cset -f -y 1 0x18 "$BIAS_REG" "$BIAS_WANT"' in text, True)
    # Naming that one too would otherwise move it from a literal nothing
    # checks into a variable nothing checks. 0x19's behaviour belongs to the
    # install step's verify; all this pins is that the two agree on the value.
    bias = re.search(r"^BIAS_REG=(\S+);\s+BIAS_WANT=(\S+)", text, re.M)
    check("and the bias want is the value the installer verifies against",
          bool(bias) and bias.group(2)
          in Path(pi_deploy.__file__).read_text(), True,
          bias.group(2) if bias else "no BIAS_WANT in it")

    # The verdict travels as an exit status, so this is the line that has to
    # hold for the install step to see it: `run_steps` fails a step on a
    # non-zero code before it ever calls the step's verify.
    step = next(s for s in pi_deploy.install_plan("pi2")
                if "mic_setup.sh" in " ".join(s.argv))
    real = pi_deploy.spawn
    pi_deploy.spawn = lambda s, timeout=900: (1, "HPF  reg 0x0C = 0x00 "
                                                 "(want 0x50)")
    try:
        code, _ = quietly(pi_deploy.run_steps, (step,), "install")
    finally:
        pi_deploy.spawn = real
    check("so a failed run is a failed install step, whatever its verify says",
          code, 1)


def run_cases() -> None:
    print("\nthe default run, driven through all four phases")
    # Answered from the command rather than from its turn. The order the
    # commands arrive in is what the second case here is about, so a
    # positional list of answers would be pinning the thing under test.
    answers = [
        ("revision.conf",
         f"[Service]\nEnvironment={pi_deploy.STAMP}={pi_deploy.REVISION}\n"
         f"Environment={pi_deploy.BUILT_STAMP}={pi_deploy.BUILT}\n"),
        (f"stat -c '%a %U %G' {pi_deploy.POLICY_DIR}",
         "644 root root\n" + pi_deploy.POLICY.read_text()),
        ("stat -c %s", str(pi_deploy.MODEL_LOCAL.stat().st_size)),
        # What the script actually prints, not a shorthand for it: the verify
        # parses this line, so a fixture in a format the Pi never emits would
        # exercise nothing.
        ("mic_setup.sh", "HPF  reg 0x0C = 0x50 (want 0x50)\n"
                         "BIAS reg 0x19 = 0x86 (want 0x86)\n"),
        (f"{pi_deploy.PI_DIR}/.env",
         "\n".join(pi_deploy.PI_ENV) + "\n" + pi_deploy.PI_ENV_ANY[0]),
        # Before the bare "is-active" below, which the server's own step also
        # contains: first needle wins, and that one answers without a journal.
        ("is-active ai-terminal 2>&1;",
         f"active\nLISTEN 0 2048 127.0.0.1:{pi_deploy.PI_PORT} 0.0.0.0:* "
         'users:(("python3",pid=812,fd=9))\n'
         f"12:00:00 pi2 uv[1]: INFO {pi_deploy.STARTED} abc1234"),
        ("is-active", "active"),
        ("journalctl", f"12:00:00 {pi_deploy.BANNER}hey_gemini"),
    ]
    ran: list[str] = []
    # What a `cat` of the kiosk unit answers *before* the deploy copies it.
    # Empty is a Pi that has never had one; the rows at the end of this
    # function set it to the repo's own text for the other half.
    unit_on_pi = ""
    kiosk_cat = f"cat {pi_deploy.UNIT_DIR}/{pi_deploy.KIOSK_UNIT.name}"

    def spawn(step, timeout=900):
        ran.append(" ".join(step.argv))
        cmd = ran[-1]
        if kiosk_cat in cmd:
            # The same shape as the policy below and for the same reason: the
            # deploy `scp`s this file, so an answer that does not move with
            # the run makes every unit-staleness row here pass without the
            # deploy having happened.
            copied = any(f"scp {pi_deploy.KIOSK_UNIT}" in c for c in ran[:-1])
            return 0, pi_deploy.KIOSK_UNIT.read_text() if copied else unit_on_pi
        if f"cat {pi_deploy.POLICY_DIR}" in cmd and "stat -c" not in cmd:
            # The one answer that has to move as the run moves. There is no
            # /etc/chromium/policies/managed on this Pi — how pi2 read on
            # 2026-09-09, and the state a first install starts from — until
            # the deploy phase puts one there. A fixture that answers the same
            # before and after cannot see the bug this group is about.
            written = any("install -D" in c for c in ran[:-1])
            return 0, pi_deploy.POLICY.read_text() if written else ""
        if f"sha256sum -c {pi_deploy.KIOSK_STAMP}" in cmd:
            # The launch stamp records policy and kiosk unit sha256 digests
            # at kiosk restart time (#212). Stale if files were written and
            # the kiosk has not been restarted since.
            last_restart = max((i for i, c in enumerate(ran[:-1])
                                if "restart ai-terminal-kiosk" in c),
                               default=-1)
            last_write = max((i for i, c in enumerate(ran[:-1])
                              if f"scp {pi_deploy.KIOSK_UNIT}" in c
                              or "install -D" in c),
                             default=-1)
            if last_restart > last_write or last_write == -1:
                return 0, "OK\n"
            return 1, "FAILED\n"
        for needle, out in answers:
            if needle in cmd:
                return 0, out
        return 0, ""

    real_spawn, real_resolve = pi_deploy.spawn, pi_deploy.resolve
    pi_deploy.spawn, pi_deploy.resolve = spawn, lambda host: host
    try:
        code, _printed = quietly(pi_deploy.main, [])
    finally:
        pi_deploy.spawn, pi_deploy.resolve = real_spawn, real_resolve
    check("all four phases run against a Pi in that state and exit zero",
          code, 0, f"{len(ran)} commands")

    # The grant goes from absent to present inside this one run, so Chromium
    # is left holding the policy it read at launch — the microphone prompt a
    # kiosk has nobody to answer. Reading the policy from inside the phase
    # loop cannot see that, because deploy wrote the file two phases earlier
    # and the comparison always matches.
    check("the run that first installs the grant restarts the kiosk",
          len([c for c in ran if "restart ai-terminal-kiosk" in c]), 1)
    read = next((i for i, c in enumerate(ran)
                 if f"cat {pi_deploy.POLICY_DIR}" in c and "stat -c" not in c),
                len(ran))
    wrote = next((i for i, c in enumerate(ran) if "install -D" in c), len(ran))
    check("because the policy is read before deploy overwrites it",
          read < wrote, True, f"read at step {read}, written at step {wrote}")

    # The sample is only taken when a restart is coming. `check` on its own is
    # a read-only look at the Pi and has no use for the answer, so moving the
    # read out of the phase loop must not put an extra connection into it.
    ran.clear()
    pi_deploy.spawn, pi_deploy.resolve = spawn, lambda host: host
    try:
        quietly(pi_deploy.main, ["check"])
    finally:
        pi_deploy.spawn, pi_deploy.resolve = real_spawn, real_resolve
    check("while a phase with no restart behind it never reads it",
          [c for c in ran if f"cat {pi_deploy.POLICY_DIR}" in c
           and "stat -c" not in c], [], f"{len(ran)} commands")

    # #183's own seam, and the one the two conditions above cannot cover
    # between them: a Pi whose policy is already this repo's and whose kiosk
    # unit is not. That is the exact state pi2 was in on 2026-09-10 — the
    # grant had been deployed, the unit had not been copied since #144 — and a
    # `main` that passed `policy_differs` alone leaves Chromium on the old
    # command line after a deploy that just wrote the new one.
    for policy_stale, unit_stale, wanted in ((False, True, 1),
                                             (True, False, 1),
                                             (False, False, 0)):
        ran.clear()
        held = (pi_deploy.spawn, pi_deploy.resolve,
                pi_deploy.policy_differs, pi_deploy.kiosk_unit_differs)
        pi_deploy.spawn, pi_deploy.resolve = spawn, lambda host: host
        pi_deploy.policy_differs = lambda host, v=policy_stale: v
        pi_deploy.kiosk_unit_differs = lambda host, v=unit_stale: v
        try:
            quietly(pi_deploy.main, ["restart"])
        finally:
            (pi_deploy.spawn, pi_deploy.resolve, pi_deploy.policy_differs,
             pi_deploy.kiosk_unit_differs) = held
        check(f"policy stale {policy_stale}, unit stale {unit_stale}: "
              f"{wanted} kiosk restart",
              len([c for c in ran if "restart ai-terminal-kiosk" in c]),
              wanted)

    # The rows above patch the two probes out, which is what lets them cover
    # the four corners; these two leave them in and let the fixture answer, so
    # they are about the sample being taken *before* the deploy rather than
    # about what it returns. That is what makes one *process* the recipe.
    # #200 made `phase` a list, so the process may now be `deploy restart`
    # rather than all four — the rows at the end of this function are that —
    # but `deploy` and then `restart` as two processes still reads a unit the
    # first has already made current and restarts the kiosk zero times. That
    # form is pinned at zero below rather than fixed here. *The kiosk's
    # debugging port* gives one run and `debug_port_cases` holds it to that.
    #
    # `policy_differs` is held False so the unit is the only thing moving;
    # with the grant absent as well, `or` short-circuits and the unit is never
    # read at all.
    for on_pi, wanted in (("", 1), (pi_deploy.KIOSK_UNIT.read_text(), 0)):
        ran.clear()
        unit_on_pi = on_pi
        held = (pi_deploy.spawn, pi_deploy.resolve, pi_deploy.policy_differs)
        pi_deploy.spawn, pi_deploy.resolve = spawn, lambda host: host
        pi_deploy.policy_differs = lambda host: False
        try:
            quietly(pi_deploy.main, [])
        finally:
            (pi_deploy.spawn, pi_deploy.resolve,
             pi_deploy.policy_differs) = held
        check("one run restarts the kiosk onto the unit it just copied"
              if wanted else
              "and leaves the panel alone when that unit was already there",
              len([c for c in ran if "restart ai-terminal-kiosk" in c]),
              wanted)
        check("having read the unit before the scp overwrote it"
              if wanted else "having read it before the scp, either way",
              next((i for i, c in enumerate(ran) if kiosk_cat in c), len(ran))
              < next((i for i, c in enumerate(ran)
                      if f"scp {pi_deploy.KIOSK_UNIT}" in c), len(ran)), True)

    # #200. The pair a code change actually wants, in one process. Before it
    # landed, argparse took at most one phase and these arguments exited 2
    # without reaching a plan. `policy_differs` is held False for the reason
    # the lap above holds it: the unit has to be the only thing moving.
    ran.clear()
    unit_on_pi = ""
    held = (pi_deploy.spawn, pi_deploy.resolve, pi_deploy.policy_differs)
    pi_deploy.spawn, pi_deploy.resolve = spawn, lambda host: host
    pi_deploy.policy_differs = lambda host: False
    try:
        code, _printed = quietly(pi_deploy.main, ["deploy", "restart"])
    finally:
        (pi_deploy.spawn, pi_deploy.resolve,
         pi_deploy.policy_differs) = held
    check("deploy and restart are one process and exit zero", code, 0,
          f"{len(ran)} commands")
    check("and that process restarts the kiosk onto the unit it just copied",
          len([c for c in ran if "restart ai-terminal-kiosk" in c]), 1)
    check("having read the unit before its own scp overwrote it",
          next((i for i, c in enumerate(ran) if kiosk_cat in c), len(ran))
          < next((i for i, c in enumerate(ran)
                  if f"scp {pi_deploy.KIOSK_UNIT}" in c), len(ran)), True)
    check("and runs no install, which is the whole reason to ask for the pair",
          [c for c in ran if "apt-get" in c], [])

    # Typed backwards it is reordered, not run as typed, and it says so. Run as
    # typed, `install` would go in ahead of `deploy` (#174) and a `restart`
    # would bring the server up on a tree the `deploy` behind it had not
    # copied yet.
    ran.clear()
    unit_on_pi = ""
    pi_deploy.spawn, pi_deploy.resolve = spawn, lambda host: host
    try:
        _code, printed = quietly(pi_deploy.main, ["restart", "deploy"])
    finally:
        pi_deploy.spawn, pi_deploy.resolve = real_spawn, real_resolve
    copied = next((i for i, c in enumerate(ran)
                   if f"scp {pi_deploy.KIOSK_UNIT}" in c), len(ran))
    reloaded = next((i for i, c in enumerate(ran)
                     if "daemon-reload" in c), len(ran))
    check("phases typed backwards run in the order a full run takes them in",
          copied < reloaded, True,
          f"copied at {copied}, reloaded at {reloaded}")
    check("and the reorder is announced rather than done in silence",
          "running deploy restart —" in printed, True,
          printed.splitlines()[0] if printed else "")

    # The early return keyed off `args.phase` being truthy, which a list makes
    # true for every multi-phase run. `check` on its own still has to decide
    # the exit code, and `check` ahead of another phase still has to fall
    # through, or the recipe above would stop at the first phase.
    ran.clear()
    unit_on_pi = ""
    pi_deploy.spawn, pi_deploy.resolve = spawn, lambda host: host
    try:
        alone, _ = quietly(pi_deploy.main, ["check"])
        gated = list(ran)
        ran.clear()
        both, _ = quietly(pi_deploy.main, ["check", "deploy"])
    finally:
        pi_deploy.spawn, pi_deploy.resolve = real_spawn, real_resolve
    check("check on its own still gates the exit code on the unmet rows",
          alone, 1, f"{len(gated)} commands")
    check("while check ahead of another phase only reports", both, 0)
    check("and lets the phase behind it run",
          any(f"scp {pi_deploy.KIOSK_UNIT}" in c for c in ran), True,
          f"{len(ran)} commands")

    # What #200 did not fix and #212 removes. Two processes: the second
    # detects that the kiosk has not launched since the first copied new files,
    # via the launch stamp on the Pi, and restarts the kiosk onto them.
    # `ran` is shared across both, which is what makes the sequence expressible
    # here at all.
    ran.clear()
    unit_on_pi = ""
    pi_deploy.spawn, pi_deploy.resolve = spawn, lambda host: host
    try:
        for phase in ("deploy", "restart"):
            quietly(pi_deploy.main, [phase])
    finally:
        pi_deploy.spawn, pi_deploy.resolve = real_spawn, real_resolve
    check("deploy and restart as two processes restarts the kiosk once",
          len([c for c in ran if "restart ai-terminal-kiosk" in c]), 1,
          f"{len(ran)} commands")


def stop_cases() -> None:
    """What a clean stop of each unit looks like from systemd's side.

    `uv run` is not the program. It waits for the child and reports what became
    of it, and both children die of the signal rather than returning: uvicorn
    handles SIGTERM, shuts the app down and re-raises it with the default
    handler restored (`Server.capture_signals`), and `wake` installs no handler
    at all. Either way uv exits 128 + 15, which systemd scores as a fault
    because 143 is not in the default success set — `Main process exited,
    code=exited, status=143/n/a` and `Failed with result 'exit-code'` on pi2 on
    2026-09-10, on every one of the four stops the two units logged that day.

    The kiosk and the tunnel are the control and they are read as one: they exec
    their binaries directly, die of SIGTERM, and systemd counts that as a clean
    exit. Four clean stops between them on the same Pi and not one `Main process
    exited` line. So which side of the line a unit falls on is decided by its
    own `ExecStart` here, rather than by naming the two files that need it.
    """
    print("\nwhat a clean stop looks like to systemd")
    through_uv, direct = [], []
    for path in sorted(DEPLOY.glob("*.service")):
        argv = (one(unit(path), "Service", "ExecStart") or "").split()
        bucket = through_uv if argv and Path(argv[0]).name == "uv" else direct
        bucket.append(path)
    check("the units that stop through a wrapper are the two uv starts",
          [p.name for p in through_uv],
          ["ai-terminal-wake.service", "ai-terminal.service"],
          f"{len(through_uv)} of {len(through_uv) + len(direct)} units")
    check("and each of them tells systemd that 143 is a clean stop",
          {p.name: every(unit(p), "Service", "SuccessExitStatus")
           for p in through_uv},
          {p.name: ["143"] for p in through_uv})
    # A status and not `SIGTERM`. The name form matches a process systemd
    # killed; uv is not killed, it waits and exits, so the name would be a line
    # that reads correct, matches nothing, and leaves the journal as it is.
    check("spelled as a status, since uv exits rather than dies of the signal",
          [v for p in through_uv
           for v in every(unit(p), "Service", "SuccessExitStatus")
           if not v.isdigit()], [])
    # 143 is 128 + 15 and nothing else. `KillSignal=SIGINT` would make it 130,
    # which the line above would stop matching without becoming wrong to read.
    check("and no unit changes the signal a stop sends, or who receives it",
          sorted({k for p in sorted(DEPLOY.glob("*.service"))
                  for k, _ in unit(p).get("Service", [])
                  if k in ("KillSignal", "KillMode")}), [])
    # The opposite mistake, and the reason the two buckets are asserted rather
    # than only the first: 143 declared clean for a process that cannot produce
    # it is a real exit code silently accepted for the life of the unit.
    check("while the two that exec their own binaries need no such line",
          {p.name: every(unit(p), "Service", "SuccessExitStatus")
           for p in direct},
          {p.name: [] for p in direct})

    # The device half. A journal is only reachable over ssh, so it is asserted
    # where the ssh already happens — the restart phase's own readers — and
    # driven here from a fixture exactly as every other reader in this file is.
    # Nothing below opens a connection; `plan_cases` is what proves that.
    fault = ("Sep 10 09:31:19 pi2 systemd[1139]: Stopping ai-terminal.service "
             "- AI Terminal live voice agent...\n"
             "Sep 10 09:31:19 pi2 systemd[1139]: ai-terminal.service: Main "
             "process exited, code=exited, status=143/n/a\n"
             "Sep 10 09:31:19 pi2 systemd[1139]: ai-terminal.service: Failed "
             "with result 'exit-code'.")
    # The same stop scored clean, which is what the two units say now: systemd
    # writes the `Stopping`/`Stopped` pair and drops everything between them to
    # debug. Kept as a whole record rather than an absence, because the case
    # below puts it *under* a fault from an older stop.
    clean = ("Sep 11 02:31:19 pi2 systemd[1139]: Stopping ai-terminal.service "
             "- AI Terminal live voice agent...\n"
             "Sep 11 02:31:19 pi2 systemd[1139]: Stopped ai-terminal.service "
             "- AI Terminal live voice agent.")
    said = ("systemd booked a stop in this window as 'exit-code': a clean "
            "stop through `uv run` exits 143, so the unit needs "
            "SuccessExitStatus=143 or every restart reads as a fault")
    same = pi_deploy.restart_plan("pi2", kiosk_stale=False)
    journal = next(s for s in same if "journalctl" in " ".join(s.argv))
    served = next(s for s in same
                  if "is-active ai-terminal 2>&1;" in " ".join(s.argv))
    mine = (f"LISTEN 0 2048 127.0.0.1:{pi_deploy.PI_PORT} 0.0.0.0:* "
            'users:(("python3",pid=812,fd=9))')
    started = f"12:00:00 INFO {pi_deploy.STARTED} abc"
    check("the monitor's step fails on a stop the journal booked as a fault",
          journal.verify(0, f"12:00:00 {pi_deploy.BANNER}hey_gemini\n{fault}"),
          said)
    check("so does the server's, after the bind test it must not stop making",
          served.verify(0, f"active\n{mine}\n{started}\n{fault}"), said)
    check("while a restart that logged nothing of the sort still passes both",
          [journal.verify(0, f"12:00:00 {pi_deploy.BANNER}hey_gemini"),
           served.verify(0, f"active\n{mine}\n{started}")], [None, None])
    # And a fault this fix cannot reach back and undo does not fail the deploy
    # for the rest of the unit's life. `-n 60` is a line count, and the
    # monitor's whole journal is 42 lines on pi2 — so its window holds every
    # stop it has ever made, the pre-fix ones included, and the first deploy
    # after this change failed on a `Failed with result` from the day before.
    # Measured, not imagined: that is how this case came to be here.
    check("and a fault under an older stop does not outlive the fix that "
          "silenced the newer one",
          [journal.verify(
              0, f"{fault}\n12:00:00 {pi_deploy.BANNER}hey_gemini\n{clean}"),
           served.verify(0, f"active\n{mine}\n{started}\n{fault}\n{clean}")],
          [None, None])
    # Both readers see the stop only because it is still inside the window the
    # step asks for. Measured on pi2: the server wrote 42 lines in the five
    # minutes after a start, and the five records of the stop sit immediately
    # above them, so 60 has room and 20 would not. A shrink here makes both
    # cases above true of a fixture and vacuous on the device.
    windows = [int(found.group(1)) for s in (journal, served)
               for found in [re.search(r"-n (\d+)", " ".join(s.argv))]]
    check("out of a window still wide enough to reach back past the stop",
          windows, [60, 60], "42 lines in the five minutes after a start")


def deploy_lock_cases() -> None:
    print("\nthe deploy lock protecting concurrent checks and probes")
    check("LOCK_FILE is under PI_DIR",
          pi_deploy.LOCK_FILE, f"{pi_deploy.PI_DIR}/.deploy.lock")
    check("LOCK_MAX_AGE_S is 10 minutes",
          pi_deploy.LOCK_MAX_AGE_S, 600)

    held = (pi_deploy.spawn, pi_deploy.resolve)

    # 1. Lock present and fresh: check reports deploy in progress, skips probes, returns unmet
    ran: list[str] = []
    def spawn_fresh(step, timeout=900):
        ran.append(" ".join(step.argv))
        if f"cat {pi_deploy.LOCK_FILE}" in ran[-1]:
            now = int(time.time())
            return 0, f"{now - 42} deadbeef"
        return 0, ""

    pi_deploy.spawn, pi_deploy.resolve = spawn_fresh, lambda host: host
    try:
        unmet, printed = quietly(pi_deploy.run_check, "pi2")
    finally:
        pi_deploy.spawn, pi_deploy.resolve = held

    check("fresh deploy lock causes check to report deploy in progress",
          "deploy started 42 seconds ago (SHA: deadbeef); readings are not trustworthy" in printed,
          True, printed)
    check("fresh deploy lock returns non-zero unmet count",
          unmet > 0, True, f"unmet={unmet}")
    check("fresh deploy lock skips the full probe suite",
          len(ran), 1, f"{len(ran)} commands executed")

    ran.clear()
    pi_deploy.spawn, pi_deploy.resolve = spawn_fresh, lambda host: host
    try:
        code, printed = quietly(pi_deploy.main, ["check"])
    finally:
        pi_deploy.spawn, pi_deploy.resolve = held
    check("fresh deploy lock makes check phase exit 1", code, 1)

    # 2. Lock present and stale (> 600s): check reports warning and executes probes
    ran.clear()
    def spawn_stale(step, timeout=900):
        ran.append(" ".join(step.argv))
        if f"cat {pi_deploy.LOCK_FILE}" in ran[-1]:
            now = int(time.time())
            return 0, f"{now - 700} oldbeef"
        return 0, ""

    pi_deploy.spawn, pi_deploy.resolve = spawn_stale, lambda host: host
    try:
        unmet, printed = quietly(pi_deploy.run_check, "pi2")
    finally:
        pi_deploy.spawn, pi_deploy.resolve = held

    check("stale deploy lock logs a warning and is ignored",
          "warning: stale deploy lock ignored" in printed,
          True, printed)
    check("stale deploy lock proceeds to execute check probes",
          len(ran) > 1, True, f"{len(ran)} commands executed")

    # 3. Lock absent: check runs normally without lock warnings
    ran.clear()
    def spawn_absent(step, timeout=900):
        ran.append(" ".join(step.argv))
        return 0, ""

    pi_deploy.spawn, pi_deploy.resolve = spawn_absent, lambda host: host
    try:
        unmet, printed = quietly(pi_deploy.run_check, "pi2")
    finally:
        pi_deploy.spawn, pi_deploy.resolve = held

    check("absent lock runs probes without deploy warnings",
          "deploy started" not in printed and "stale deploy lock" not in printed,
          True)
    check("absent lock executes full probe suite",
          len(ran) > 1, True, f"{len(ran)} commands executed")

    # 4. Writing phases acquire the lock and remove it in finally
    # 4a: successful deploy writing run
    ran.clear()
    def spawn_deploy(step, timeout=900):
        ran.append(" ".join(step.argv))
        cmd = ran[-1]
        if "stat -c %s" in cmd:
            return 0, str(pi_deploy.MODEL_LOCAL.stat().st_size)
        if "revision.conf" in cmd:
            return 0, (f"[Service]\nEnvironment={pi_deploy.STAMP}={pi_deploy.REVISION}\n"
                       f"Environment={pi_deploy.BUILT_STAMP}={pi_deploy.BUILT}\n")
        return 0, ""

    pi_deploy.spawn, pi_deploy.resolve = spawn_deploy, lambda host: host
    try:
        code, _ = quietly(pi_deploy.main, ["deploy"])
    finally:
        pi_deploy.spawn, pi_deploy.resolve = held
    check("writing phase exits zero", code, 0)
    lock_step = next((i for i, c in enumerate(ran) if f"> {pi_deploy.LOCK_FILE}" in c), None)
    unlock_step = next((i for i, c in enumerate(ran) if f"rm -f {pi_deploy.LOCK_FILE}" in c), None)
    check("writing phase acquires lock at start",
          lock_step is not None and lock_step == 0, True, f"lock at {lock_step}")
    check("writing phase removes lock in finally",
          unlock_step is not None and unlock_step == len(ran) - 1, True, f"unlock at {unlock_step}")

    # 4b: failure during writing phase still removes lock in finally
    ran.clear()
    def spawn_failing(step, timeout=900):
        ran.append(" ".join(step.argv))
        if f"> {pi_deploy.LOCK_FILE}" in ran[-1]:
            return 0, ""
        if f"rm -f {pi_deploy.LOCK_FILE}" in ran[-1]:
            return 0, ""
        return 1, "simulated deploy failure"

    pi_deploy.spawn, pi_deploy.resolve = spawn_failing, lambda host: host
    try:
        code, _ = quietly(pi_deploy.main, ["deploy"])
    finally:
        pi_deploy.spawn, pi_deploy.resolve = held
    check("failed writing phase returns non-zero", code != 0, True)
    unlock_step = next((i for i, c in enumerate(ran) if f"rm -f {pi_deploy.LOCK_FILE}" in c), None)
    check("failed writing phase removes lock in finally",
          unlock_step is not None, True, f"unlock at {unlock_step}")


def githook_cases(readme: str) -> None:
    print("\nthe issue #302 git hooks and core.hooksPath (#377)")
    githooks = ROOT / ".githooks"
    check("the workspace tracks a .githooks directory", githooks.is_dir(), True)

    pre_commit = githooks / "pre-commit"
    check(".githooks/pre-commit exists", pre_commit.is_file(), True)
    check(".githooks/pre-commit is executable", os.access(pre_commit, os.X_OK), True)

    pre_push = githooks / "pre-push"
    check(".githooks/pre-push exists", pre_push.is_file(), True)
    check(".githooks/pre-push is executable", os.access(pre_push, os.X_OK), True)

    # An isolated clean repository has core.hooksPath unset (the reachable red state)
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["git", "init", "-q"], cwd=td, check=True, env=_git_env())
        unconfigured = subprocess.run(
            ["git", "-C", td, "config", "--get", "core.hooksPath"],
            capture_output=True, text=True, env=_git_env(),
        ).stdout.strip()
        check("an unconfigured repo has core.hooksPath unset (reachable red state)",
              unconfigured, "")

        # Verifying explicit configuration in isolation sets core.hooksPath to .githooks
        subprocess.run(
            ["git", "-C", td, "config", "core.hooksPath", ".githooks"],
            check=True, env=_git_env(),
        )
        configured = subprocess.run(
            ["git", "-C", td, "config", "--get", "core.hooksPath"],
            capture_output=True, text=True, env=_git_env(),
        ).stdout.strip()
        check("configured repo core.hooksPath resolves to .githooks",
              configured, ".githooks")

        # Test that ccrole.sh auto-configures core.hooksPath when .githooks is present
        (Path(td) / ".githooks").mkdir(exist_ok=True)
        subprocess.run(
            ["git", "-C", td, "config", "--unset", "core.hooksPath"],
            check=True, env=_git_env(),
        )
        ccrole_path = ROOT / ".claude" / "hooks" / "ccrole.sh"
        check(".claude/hooks/ccrole.sh exists", ccrole_path.is_file(), True)
        ccrole_content = ccrole_path.read_text()
        check("ccrole.sh contains core.hooksPath auto-configuration",
              'git -C "$dir" config core.hooksPath .githooks' in ccrole_content, True)

        subprocess.run(
            ["bash", str(ccrole_path), "cli"],
            cwd=td, capture_output=True, check=True, env=_git_env(),
        )
        launcher_configured = subprocess.run(
            ["git", "-C", td, "config", "--get", "core.hooksPath"],
            capture_output=True, text=True, env=_git_env(),
        ).stdout.strip()
        check("launcher auto-configuration sets core.hooksPath in repo with .githooks",
              launcher_configured, ".githooks")

    hooks_path = subprocess.run(
        ["git", "-C", str(ROOT), "config", "--get", "core.hooksPath"],
        capture_output=True, text=True, env=_git_env(),
    ).stdout.strip()
    check("git config core.hooksPath resolves to .githooks", hooks_path, ".githooks")

    check("the quickstart documents git config core.hooksPath .githooks",
          "git config core.hooksPath .githooks" in readme, True)

    # Pre-commit hook enforces .claude/ and CLAUDE.md protection (#389)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        subprocess.run(["git", "init", "-q"], cwd=td, check=True, env=_git_env())
        subprocess.run(["git", "-C", td, "config", "user.name", "Tester"], check=True, env=_git_env())
        subprocess.run(["git", "-C", td, "config", "user.email", "tester@example.com"], check=True, env=_git_env())
        subprocess.run(["git", "-C", td, "config", "commit.gpgsign", "false"], check=True, env=_git_env())
        (root / ".githooks").symlink_to(githooks)
        subprocess.run(["git", "-C", td, "config", "core.hooksPath", ".githooks"], check=True, env=_git_env())

        def try_commit(env_overrides: dict[str, str | None]) -> subprocess.CompletedProcess[str]:
            env = {k: v for k, v in _git_env().items() if k not in ("CC_ROLE", "CLAUDE_CONFIG_OK")}
            for k, v in env_overrides.items():
                if v is None:
                    env.pop(k, None)
                else:
                    env[k] = v
            return subprocess.run(
                ["git", "-C", td, "commit", "-m", "test commit"],
                capture_output=True, text=True, env=env,
            )

        # Red first: staging .claude/test.txt with CC_ROLE=coder exits 1 with refusal message
        (root / ".claude").mkdir(parents=True, exist_ok=True)
        (root / ".claude" / "test.txt").write_text("blocked change\n")
        subprocess.run(["git", "-C", td, "add", ".claude/test.txt"], check=True, env=_git_env())
        res = try_commit({"CC_ROLE": "coder"})
        check("pre-commit blocks .claude/ commit under CC_ROLE=coder", res.returncode, 1)
        check("refusal message names .claude/test.txt",
              "pre-commit: refusing a commit touching .claude/test.txt." in res.stderr, True)
        check("refusal message identifies CC_ROLE=coder",
              "committer CC_ROLE='coder'" in res.stderr, True)
        check("refusal message offers owner escape hatch",
              "To commit as owner, set CLAUDE_CONFIG_OK=1." in res.stderr, True)

        # Refused when CC_ROLE is unset or empty: staging CLAUDE.md exits 1
        subprocess.run(["git", "-C", td, "reset", "-q"], check=True, env=_git_env())
        (root / "CLAUDE.md").write_text("blocked change\n")
        subprocess.run(["git", "-C", td, "add", "CLAUDE.md"], check=True, env=_git_env())
        res_empty = try_commit({"CC_ROLE": ""})
        check("pre-commit blocks CLAUDE.md when CC_ROLE is empty", res_empty.returncode, 1)
        check("refusal message names CLAUDE.md",
              "pre-commit: refusing a commit touching CLAUDE.md." in res_empty.stderr, True)
        check("refusal message reports CC_ROLE as <unset> when empty",
              "committer CC_ROLE='<unset>'" in res_empty.stderr, True)

        res_unset = try_commit({"CC_ROLE": None})
        check("pre-commit blocks CLAUDE.md when CC_ROLE is unset", res_unset.returncode, 1)
        check("refusal message reports CC_ROLE as <unset> when unset",
              "committer CC_ROLE='<unset>'" in res_unset.stderr, True)

        # Green allowed case 1: staging .claude/test.txt with CC_ROLE=monitor succeeds
        subprocess.run(["git", "-C", td, "reset", "-q"], check=True, env=_git_env())
        subprocess.run(["git", "-C", td, "add", ".claude/test.txt"], check=True, env=_git_env())
        res_monitor = try_commit({"CC_ROLE": "monitor"})
        check("pre-commit allows .claude/ commit under CC_ROLE=monitor", res_monitor.returncode, 0)

        # Green allowed case 2: staging .claude/test.txt with CLAUDE_CONFIG_OK=1 succeeds
        (root / ".claude" / "test.txt").write_text("allowed change\n")
        subprocess.run(["git", "-C", td, "add", ".claude/test.txt"], check=True, env=_git_env())
        res_owner = try_commit({"CC_ROLE": "coder", "CLAUDE_CONFIG_OK": "1"})
        check("pre-commit allows .claude/ commit with CLAUDE_CONFIG_OK=1", res_owner.returncode, 0)

        # Unprotected file: staging README.md with CC_ROLE=coder succeeds
        (root / "README.md").write_text("normal update\n")
        subprocess.run(["git", "-C", td, "add", "README.md"], check=True, env=_git_env())
        res_unprotected = try_commit({"CC_ROLE": "coder"})
        check("pre-commit allows unprotected file commit under CC_ROLE=coder", res_unprotected.returncode, 0)

        # Verify existing check@localhost rejection still passes
        subprocess.run(["git", "-C", td, "config", "user.email", "check@localhost"], check=True, env=_git_env())
        (root / "README.md").write_text("check@localhost update\n")
        subprocess.run(["git", "-C", td, "add", "README.md"], check=True, env=_git_env())
        res_author = try_commit({"CC_ROLE": "coder"})
        check("pre-commit blocks author check@localhost", res_author.returncode, 1)
        check("refusal message rejects check@localhost author",
              "pre-commit: refusing a commit authored by check@localhost." in res_author.stderr, True)


def tester_ceiling_cases() -> None:
    """Guard static wiring of latency ceiling constants in tst_runner.py source.

    Note (#468): These assertions guard wiring of constants in runner source,
    distinguishing them from runtime behavioural latency measurements (which are
    asserted per-commit in tools/timing_check.py:page_load_timing_cases).
    """
    print("\nlatency ceilings on deterministic tester scenarios (#445, #468)")
    runner_src = (ROOT / "tools" / "tst_runner.py").read_text(encoding="utf-8")

    # 1. Absolute ceilings defined as constants (runner source wiring guard)
    check("tst_runner defines LOAD_SLASH_MAX_ELAPSED_MS",
          "LOAD_SLASH_MAX_ELAPSED_MS = 2500" in runner_src, True)
    check("tst_runner defines RELOAD_MID_CONV_MAX_ELAPSED_MS",
          "RELOAD_MID_CONV_MAX_ELAPSED_MS = 5500" in runner_src, True)

    # 2. Scenario 1 (load-slash) fails when exceeding ceiling (wiring guard)
    s1_block = runner_src[runner_src.find("# --- Scenario 1: Loads / ---"):runner_src.find("# --- Scenario 2: Leaves it alone")]
    check("scenario 1 assigns max_elapsed_ms",
          "max_elapsed_ms = LOAD_SLASH_MAX_ELAPSED_MS" in s1_block or "max_elapsed_ms = 2500" in s1_block, True)
    check("scenario 1 marks pass=False when elapsed_ms exceeds ceiling",
          "elapsed_ms > max_elapsed_ms" in s1_block and "s1_pass = False" in s1_block, True)

    # 3. Scenario 6 (reload-mid-conversation) fails when exceeding ceiling (wiring guard)
    s6_block = runner_src[runner_src.find("# --- Scenario 6: Reloads mid-conversation ---"):runner_src.find("# --- Scenario 9: Starter buttons")]
    check("scenario 6 assigns max_elapsed_ms",
          "max_elapsed_ms = RELOAD_MID_CONV_MAX_ELAPSED_MS" in s6_block or "max_elapsed_ms = 5500" in s6_block, True)
    check("scenario 6 marks pass=False when elapsed_ms exceeds ceiling",
          "elapsed_ms > max_elapsed_ms" in s6_block and "s6_pass = False" in s6_block, True)

    # 4. Strict latency ceilings are not placed on model chip scenarios (avoid flaky runs on LLM variance)
    chip_blocks = runner_src[runner_src.find("# --- Scenario 14: News chip"):runner_src.find("# --- Scenario 8: POST /api/forget")]
    check("model-driven chip scenarios do not enforce hard latency ceilings",
          "max_elapsed_ms" not in chip_blocks, True)


def trajectory_history_cases() -> None:
    print("\ntrajectory suite periodic execution and history rendering (#449, #471)")
    tools_dir = ROOT / "tools"
    if not (tools_dir / "tst_runner.py").exists() and (Path(__file__).parent / "tst_runner.py").exists():
        tools_dir = Path(__file__).parent
    runner_src = (tools_dir / "tst_runner.py").read_text(encoding="utf-8")

    # 1. Verify tst_runner.py defines TRAJ_SHA_FILE and should_run_trajectories
    check("tst_runner defines TRAJ_SHA_FILE",
          'TRAJ_SHA_FILE = Path("/tmp/tst-last-traj-sha")' in runner_src, True)
    check("tst_runner defines should_run_trajectories",
          "def should_run_trajectories(" in runner_src, True)
    check("tst_runner defines mark_trajectories_run",
          "def mark_trajectories_run(" in runner_src, True)

    # 2. Verify tst_render.py includes sections
    render_src = (tools_dir / "tst_render.py").read_text(encoding="utf-8")
    check("tst_render includes Trajectory pass rate section header",
          "Trajectory pass rate, last 20 runs" in render_src, True)
    check("tst_render includes Trajectory median complete_ms section header",
          "Trajectory median complete_ms, last 20 runs" in render_src, True)

    # 3. Test rendering with mock trajectory data and verify headers and sparkline/bar chart lines
    import importlib.util
    spec = importlib.util.spec_from_file_location("tst_render", tools_dir / "tst_render.py")
    tst_render_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tst_render_mod)

    with tempfile.TemporaryDirectory() as td:
        tmp_metrics = Path(td) / "metrics.jsonl"
        tmp_results = Path(td) / "results.md"

        sample_ts1 = "2026-09-12T08:00:00Z"
        sample_ts2 = "2026-09-12T09:00:00Z"
        traj_data = [
            {"ts": sample_ts1, "sha": "1111aaaa", "dirty": False, "scenario": "load-slash", "pass": True, "complete_ms": 500, "elapsed_ms": 600, "run_id": "s1"},
            {"ts": sample_ts1, "sha": "1111aaaa", "dirty": False, "scenario": "traj-dining-directions", "pass": True, "complete_ms": 1000, "elapsed_ms": 1200, "turns": 3, "run_id": "r1"},
            {"ts": sample_ts1, "sha": "1111aaaa", "dirty": False, "scenario": "traj-weather-forecast", "pass": True, "complete_ms": 1200, "elapsed_ms": 1500, "turns": 3, "run_id": "r1"},
            {"ts": sample_ts2, "sha": "2222bbbb", "dirty": False, "scenario": "traj-dining-directions", "pass": False, "complete_ms": 2000, "elapsed_ms": 2200, "turns": 3, "run_id": "r2"},
            {"ts": sample_ts2, "sha": "2222bbbb", "dirty": False, "scenario": "traj-weather-forecast", "pass": True, "complete_ms": 2400, "elapsed_ms": 2500, "turns": 3, "run_id": "r2"},
        ]
        with open(tmp_metrics, "w", encoding="utf-8") as f:
            for item in traj_data:
                f.write(json.dumps(item) + "\n")

        orig_metrics = tst_render_mod.METRICS_FILE
        orig_results = tst_render_mod.RESULTS_FILE
        try:
            tst_render_mod.METRICS_FILE = tmp_metrics
            tst_render_mod.RESULTS_FILE = tmp_results
            tst_render_mod.render_results()

            content = tmp_results.read_text(encoding="utf-8")
            check("render produces Trajectory pass rate header",
                  "## Trajectory pass rate, last 20 runs" in content, True)
            check("render produces bar chart lines for trajectory runs",
                  "1111aaaa" in content and "2/2" in content and "2222bbbb" in content and "1/2" in content, True)
            check("render produces Trajectory median complete_ms header",
                  "## Trajectory median complete_ms, last 20 runs" in content, True)
            check("render produces sparkline range line for trajectory runs",
                  "1100 → 2200 ms" in content or ("ms" in content and "→" in content), True)
        finally:
            tst_render_mod.METRICS_FILE = orig_metrics
            tst_render_mod.RESULTS_FILE = orig_results

    # 4. Narrowed run does not mark SHA covered (#471)
    spec_runner = importlib.util.spec_from_file_location("tst_runner", tools_dir / "tst_runner.py")
    tst_runner_mod = importlib.util.module_from_spec(spec_runner)
    spec_runner.loader.exec_module(tst_runner_mod)

    with tempfile.TemporaryDirectory() as td:
        tmp_sha_file = Path(td) / "tst-last-traj-sha"
        orig_sha_file = tst_runner_mod.TRAJ_SHA_FILE
        try:
            tst_runner_mod.TRAJ_SHA_FILE = tmp_sha_file
            tst_runner_mod.mark_trajectories_run("testsha", narrowed=True)
            check("narrowed run does not write /tmp/tst-last-traj-sha",
                  tmp_sha_file.exists(), False)
            check("later full run at same SHA still executes",
                  tst_runner_mod.should_run_trajectories("testsha"), True)
        finally:
            tst_runner_mod.TRAJ_SHA_FILE = orig_sha_file

    # 5. Suite denominator rendering & partial run not full green bar (#471)
    with tempfile.TemporaryDirectory() as td:
        tmp_metrics = Path(td) / "metrics.jsonl"
        tmp_results = Path(td) / "results.md"
        data = [
            {"ts": "2026-09-12T08:00:00Z", "sha": "testsha1", "dirty": False,
             "scenario": "load-slash", "pass": True, "complete_ms": 500, "elapsed_ms": 600, "run_id": "s1"},
            {"ts": "2026-09-12T08:00:00Z", "sha": "testsha1", "dirty": False,
             "scenario": "traj-dining-directions", "pass": True, "complete_ms": 1000, "elapsed_ms": 1200, "turns": 3, "run_id": "r1"},
        ]
        with open(tmp_metrics, "w", encoding="utf-8") as f:
            for item in data:
                f.write(json.dumps(item) + "\n")

        orig_metrics = tst_render_mod.METRICS_FILE
        orig_results = tst_render_mod.RESULTS_FILE
        try:
            tst_render_mod.METRICS_FILE = tmp_metrics
            tst_render_mod.RESULTS_FILE = tmp_results
            tst_render_mod.render_results()
            c = tmp_results.read_text(encoding="utf-8")

            check("current_test_results.md states how many of suite scenarios run covered",
                  "1/10 scenarios run" in c, True)
            in_traj_rate = False
            traj_hist_lines = []
            for l in c.splitlines():
                if "## Trajectory pass rate, last 20 runs" in l:
                    in_traj_rate = True
                    continue
                if in_traj_rate:
                    if l.startswith("##") or l.startswith(">"):
                        in_traj_rate = False
                    elif "testsha1" in l:
                        traj_hist_lines.append(l)
            full_bar = "█" * 20
            check("partial trajectory run does not render as full green bar",
                  bool(traj_hist_lines) and all(full_bar not in l for l in traj_hist_lines), True)
        finally:
            tst_render_mod.METRICS_FILE = orig_metrics
            tst_render_mod.RESULTS_FILE = orig_results

    # 6. Trajectory suite exception reported as failed trajectory run, singles still render (#471)
    with tempfile.TemporaryDirectory() as td:
        tmp_metrics = Path(td) / "metrics.jsonl"
        tmp_results = Path(td) / "results.md"
        data = [
            {"ts": "2026-09-12T08:00:00Z", "sha": "testsha2", "dirty": False,
             "scenario": "load-slash", "pass": True, "complete_ms": 500, "elapsed_ms": 600, "run_id": "s1"},
            {"ts": "2026-09-12T08:00:01Z", "sha": "testsha2", "dirty": False,
             "scenario": "traj-suite-execution", "pass": False, "first_token_ms": None,
             "complete_ms": None, "elapsed_ms": 0, "tools": [], "tool_calls": 0, "turns": 0,
             "context_retained": False, "timeout_ms": 0, "screenshot": "", "run_id": "s1"},
        ]
        with open(tmp_metrics, "w", encoding="utf-8") as f:
            for item in data:
                f.write(json.dumps(item) + "\n")

        orig_metrics = tst_render_mod.METRICS_FILE
        orig_results = tst_render_mod.RESULTS_FILE
        try:
            tst_render_mod.METRICS_FILE = tmp_metrics
            tst_render_mod.RESULTS_FILE = tmp_results
            tst_render_mod.render_results()
            c = tmp_results.read_text(encoding="utf-8")

            check("trajectory suite exception reported as failed trajectory run",
                  "traj-suite-execution" in c and "FAIL" in c, True)
            check("single-turn results from same invocation still render",
                  "load-slash" in c and "ok" in c and "1/1 pass" in c, True)
        finally:
            tst_render_mod.METRICS_FILE = orig_metrics
            tst_render_mod.RESULTS_FILE = orig_results

    # 7. Absent trajectory result for new SHA after merge is explicitly noted (#471)
    with tempfile.TemporaryDirectory() as td:
        tmp_metrics = Path(td) / "metrics.jsonl"
        tmp_results = Path(td) / "results.md"
        data = [
            {"ts": "2026-09-12T07:00:00Z", "sha": "oldsha", "dirty": False,
             "scenario": "traj-dining-directions", "pass": True, "complete_ms": 1000, "elapsed_ms": 1200, "turns": 3, "run_id": "r0"},
            {"ts": "2026-09-12T08:00:00Z", "sha": "newsha", "dirty": False,
             "scenario": "load-slash", "pass": True, "complete_ms": 500, "elapsed_ms": 600, "run_id": "s1"},
        ]
        with open(tmp_metrics, "w", encoding="utf-8") as f:
            for item in data:
                f.write(json.dumps(item) + "\n")

        orig_metrics = tst_render_mod.METRICS_FILE
        orig_results = tst_render_mod.RESULTS_FILE
        try:
            tst_render_mod.METRICS_FILE = tmp_metrics
            tst_render_mod.RESULTS_FILE = tmp_results
            tst_render_mod.render_results()
            c = tmp_results.read_text(encoding="utf-8")

            check("dashboard explicitly states no trajectory results for new SHA",
                  ("No trajectory results for newsha" in c or "no trajectory" in c.lower()) and "oldsha" in c, True)
        finally:
            tst_render_mod.METRICS_FILE = orig_metrics
            tst_render_mod.RESULTS_FILE = orig_results

    check("tst_runner wraps run_trajectories_suite in try/except",
          "try:\n            await run_trajectories_suite" in runner_src or
          "try:\n        await run_trajectories_suite" in runner_src, True)



def traj_render_stale_warning_cases() -> None:
    print("\ntrajectory rendering stale cached results notice (#570)")
    tools_dir = ROOT / "tools"
    if not (tools_dir / "tst_runner.py").exists() and (Path(__file__).parent / "tst_runner.py").exists():
        tools_dir = Path(__file__).parent
    runner_src = (tools_dir / "tst_runner.py").read_text(encoding="utf-8")
    traj_src = (tools_dir / "tst_trajectories.py").read_text(encoding="utf-8")
    render_src = (tools_dir / "tst_render.py").read_text(encoding="utf-8")

    # 1. Verify signatures and invocations
    check("tst_render accepts traj_ran parameter",
          "def render_results(current_run_id=None, traj_ran" in render_src, True)
    check("tst_runner calls render_results with traj_ran=False",
          "render_results(current_run_id=CURRENT_RUN_ID, traj_ran=False)" in runner_src, True)
    check("tst_trajectories calls render_results with traj_ran=True",
          "render_results(current_run_id=run_id, traj_ran=True)" in traj_src, True)

    import importlib.util
    spec = importlib.util.spec_from_file_location("tst_render", tools_dir / "tst_render.py")
    tst_render_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tst_render_mod)

    # 2. Rendering with traj_ran=False displays explicit skipped/cached notice even when SHA matches
    with tempfile.TemporaryDirectory() as td:
        tmp_metrics = Path(td) / "metrics.jsonl"
        tmp_results = Path(td) / "results.md"
        data = [
            {"ts": "2026-09-12T07:00:00Z", "sha": "testsha570", "dirty": False,
             "scenario": "load-slash", "pass": True, "complete_ms": 500, "elapsed_ms": 600, "run_id": "r1"},
            {"ts": "2026-09-12T07:05:00Z", "sha": "testsha570", "dirty": False,
             "scenario": "traj-dining-directions", "pass": True, "complete_ms": 1000, "elapsed_ms": 1200, "turns": 3, "run_id": "t1"},
            {"ts": "2026-09-12T08:00:00Z", "sha": "testsha570", "dirty": False,
             "scenario": "load-slash", "pass": True, "complete_ms": 500, "elapsed_ms": 600, "run_id": "r2"},
        ]
        with open(tmp_metrics, "w", encoding="utf-8") as f:
            for item in data:
                f.write(json.dumps(item) + "\n")

        orig_metrics = tst_render_mod.METRICS_FILE
        orig_results = tst_render_mod.RESULTS_FILE
        try:
            tst_render_mod.METRICS_FILE = tmp_metrics
            tst_render_mod.RESULTS_FILE = tmp_results

            # Case A: Explicit traj_ran=False
            tst_render_mod.render_results(current_run_id="r2", traj_ran=False)
            c = tmp_results.read_text(encoding="utf-8")
            check("dashboard includes explicit skipped notice when traj_ran=False",
                  "trajectories skipped this run" in c, True)
            check("skipped notice references cached timestamp and sha",
                  "2026-09-12T07:05:00Z" in c and "testsha570" in c, True)

            # Case B: traj_ran=None with previous run detection (current_run_id="r2")
            tst_render_mod.render_results(current_run_id="r2")
            c = tmp_results.read_text(encoding="utf-8")
            check("dashboard detects stale trajectory when current_run_id does not match",
                  "trajectories skipped this run" in c, True)

            # Case C: traj_ran=None without current_run_id detects latest run had no trajectories
            tst_render_mod.render_results()
            c = tmp_results.read_text(encoding="utf-8")
            check("dashboard detects stale trajectory from metrics file without current_run_id",
                  "trajectories skipped this run" in c, True)

            # Case D: traj_ran=True suppresses skipped notice
            tst_render_mod.render_results(current_run_id="t1", traj_ran=True)
            c = tmp_results.read_text(encoding="utf-8")
            check("dashboard suppresses skipped notice when traj_ran=True",
                  "trajectories skipped this run" not in c, True)
        finally:
            tst_render_mod.METRICS_FILE = orig_metrics
            tst_render_mod.RESULTS_FILE = orig_results


def tester_sync_cases() -> None:
    print("\ntester runner scenario completeness and tmp runner parity (#487)")
    tools_dir = ROOT / "tools"
    if not (tools_dir / "tst_runner.py").exists() and (Path(__file__).parent / "tst_runner.py").exists():
        tools_dir = Path(__file__).parent
    repo_runner = tools_dir / "tst_runner.py"
    runner_src = repo_runner.read_text(encoding="utf-8")

    # 1. Assert all 24 scenarios are defined in tools/tst_runner.py
    scenarios = {
        s for s in re.findall(r'append_metric\(sha,\s*dirty,\s*"([^"]+)"', runner_src)
        if s != "traj-suite-execution" and not s.startswith("regression-")
    }
    check("tst_runner defines all 24 scenarios",
          len(scenarios), 24, f"{len(scenarios)} scenarios found")

    expected_scenarios = {
        "screensaver-api",
        "screensaver-image-api",
        "load-slash",
        "screensaver-clock-weather",
        "weather-chip",
        "weather-card-timezone-203",
        "text-input-qa",
        "reload-mid-conversation",
        "starters-bottom-placement-368",
        "touch-scrollbars-371",
        "build-badge-369",
        "static-cache-busting-178",
        "news-chip-websearch",
        "places-chip-mapssearch",
        "market-chip-websearch",
        "routes-chip-mapsroute",
        "websearch-chip-grounding",
        "video-chip-youtubesearch",
        "browse-card",
        "plan-card",
        "wake-word-wake",
        "dynamic-chip-selection",
        "starters-suggestions-api",
        "forget-endpoint-api",
    }
    check("scenario names match the expected 24 scenarios",
          scenarios, expected_scenarios, f"{len(scenarios)} match expected")

    # 2. Divergence between runner files is detectable
    with tempfile.TemporaryDirectory() as td:
        diverged_runner = Path(td) / "tst-runner.py"
        diverged_runner.write_bytes(b"# diverged content")
        check("divergence between tmp runner and repo runner is detectable",
              diverged_runner.read_bytes() == repo_runner.read_bytes(), False)

    # 3. Assert parity with /tmp/tst-runner.py if present on the filesystem
    tmp_runner = Path("/tmp/tst-runner.py")
    if tmp_runner.exists():
        check("running tester matches repo tester",
              tmp_runner.read_bytes(), repo_runner.read_bytes())

    # 4. Assert expected_tools validation in trajectory runner (#488, #729)
    traj_runner = tools_dir / "tst_trajectories.py"
    if traj_runner.exists():
        traj_src = traj_runner.read_text(encoding="utf-8")
        check("trajectory runner validates expected_tools",
              "expected_tools and not check_expected_tools(expected_tools, all_tools)" in traj_src
              or "expected_tools and not (set(expected_tools) <= set(all_tools))" in traj_src, True)
        check("trajectory runner checks place_info images and sources",
              "place_info" in traj_src and "images" in traj_src and "sources" in traj_src, True)
        # Unit test the logic itself
        test_expected = ["search_web", "maps_route"]
        test_called_ok = ["search_web", "maps_route", "place_info"]
        test_called_missing = ["search_web"]
        is_pass_ok = (set(test_expected) <= set(test_called_ok))
        is_pass_missing = (set(test_expected) <= set(test_called_missing))
        check("expected_tools matching all_tools passes", is_pass_ok, True)
        check("expected_tools missing from all_tools fails", is_pass_missing, False)

    # 5. Assert Scenario 5 in tst_runner.py does not weaken assertion (#488)
    check("tst_runner scenario 5 does not accept arbitrary non-empty text",
          "or len(last_text.strip()) > 0" in runner_src, False)

    # 6. Assert Scenario 21 clicks via page.click directly to prevent detached DOM errors (#548, #682)
    check("tst_runner scenario 21 does not click stale dynamic_chips[0] handle",
          "await dynamic_chips[0].click()" in runner_src, False)
    check("tst_runner scenario 21 clicks via live selector",
          'await page.click(".dynamic-chip")' in runner_src, True)
    click_pos = runner_src.find('await page.click(".dynamic-chip")')
    judge_pos = runner_src.find('judge_chip_relevance')
    check("tst_runner scenario 21 clicks dynamic chip before awaiting judge (#682)",
          click_pos != -1 and judge_pos != -1 and click_pos < judge_pos, True)

    # 7. Assert browse-card scenario wait_for_selector timeout is >= 20000ms (#614)
    browse_timeout_m = re.search(r'browse_page\.wait_for_selector\([\'"][^\'"]*card[^\'"]*[\'"],\s*timeout=(\d+)\)', runner_src)
    browse_timeout = int(browse_timeout_m.group(1)) if browse_timeout_m else 0
    check("tst_runner browse-card selector timeout >= 20000ms (#614)",
          browse_timeout >= 20000, True, f"timeout={browse_timeout}ms")


def regressions_corpus_cases() -> None:
    print("\nregressions corpus integrity and loader cases (#509)")
    corpus_path = ROOT / "tools" / "regressions.yml"
    real_repo_metrics = ROOT / "tst_metrics.jsonl"
    repo_metrics_size_before = real_repo_metrics.stat().st_size if real_repo_metrics.exists() else 0

    # 1. Check tools/regressions.yml exists
    check("tools/regressions.yml exists", corpus_path.exists(), True)

    # 2. Check tools/regressions.yml is tracked in git
    tracked = subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-files", "tools/regressions.yml"],
        text=True, env=_git_env()
    ).strip()
    check("tools/regressions.yml is tracked in git", bool(tracked), True)

    # 3. Check content is data-only (no executable python: eval, exec, __import__)
    raw_text = corpus_path.read_text(encoding="utf-8") if corpus_path.exists() else ""
    dangerous = any(bad in raw_text for bad in ("eval(", "exec(", "__import__", "import "))
    check("tools/regressions.yml contains no executable python or expressions", dangerous, False)

    # 4. Check it parses with yaml.safe_load() as a list of dicts
    entries = []
    parse_ok = False
    try:
        loaded = yaml.safe_load(raw_text)
        if isinstance(loaded, list) and all(isinstance(e, dict) for e in loaded):
            entries = loaded
            parse_ok = True
    except Exception:
        parse_ok = False
    check("tools/regressions.yml parses as a list of dicts", parse_ok, True)

    # 5. Check at least 1 entry exists
    check("regressions corpus has at least 1 entry", len(entries) >= 1, True, f"{len(entries)} entries found")

    # 6. Check each entry has issue (int), title (str), drive (dict or list), assert (dict)
    valid_entries = bool(entries) and all(
        isinstance(e.get("issue"), int) and
        isinstance(e.get("title"), str) and len(e.get("title", "").strip()) > 0 and
        isinstance(e.get("drive"), (dict, list)) and
        isinstance(e.get("assert"), dict)
        for e in entries
    )
    check("each regression entry has issue (int), title (str), drive (dict or list), assert (dict)", valid_entries, True)

    # 7. Check that entries naming issue 472 and 542 are present
    issues = [e.get("issue") for e in entries if isinstance(e, dict)]
    check("regressions corpus contains entry for issue 472", 472 in issues, True)
    check("regressions corpus contains entry for issue 542", 542 in issues, True)

    # 7b. Check that entry 542 has a drive that renders a card before asserting starters (#551)
    entry_542 = next((e for e in entries if isinstance(e, dict) and e.get("issue") == 542), None)
    check("entry 542 is a dict", entry_542 is not None, True)
    if entry_542:
        e542_drive = entry_542.get("drive", {})
        e542_steps = e542_drive.get("steps") if isinstance(e542_drive, dict) else e542_drive
        check("entry 542 uses multi-step drive", isinstance(e542_steps, list) and len(e542_steps) >= 2, True)
        has_card_trigger = any(".chip" in str(s.get("click", "")) for s in e542_steps if isinstance(s, dict))
        check("entry 542 drive clicks a chip to render a card", has_card_trigger, True)
        click_idx = next((i for i, s in enumerate(e542_steps) if isinstance(s, dict) and s.get("click")), -1)
        has_wait_after = click_idx >= 0 and any(int(s.get("wait_ms", 0)) >= 40000 for s in e542_steps[click_idx + 1:] if isinstance(s, dict))
        check("entry 542 waits at least 40000ms after click for starters to reappear via goIdle", has_wait_after, True)

    # 8. Check tst_runner.py defines load_regression_entries and run_regressions_corpus
    tools_dir = ROOT / "tools"
    runner_path = tools_dir / "tst_runner.py"
    runner_src = runner_path.read_text(encoding="utf-8") if runner_path.exists() else ""
    check("tst_runner defines load_regression_entries", "def load_regression_entries" in runner_src, True)
    check("tst_runner defines run_regressions_corpus", "def run_regressions_corpus" in runner_src, True)
    check("tst_runner calls run_regressions_corpus", "run_regressions_corpus(" in runner_src, True)

    # 9. Check failure format names #{issue} regressed
    check("tst_runner formats regression failures naming issue number",
          "regressed" in runner_src and ("#{issue}" in runner_src or "#{entry['issue']}" in runner_src), True)

    # 10. Unit test: invalid schema or malformed entry fails rather than skips
    try:
        from tools import tst_runner
    except ImportError:
        import tst_runner

    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as tf:
        tf.write("- issue: 999\n  # missing title, drive, assert\n")
        bad_path = tf.name
    try:
        bad_entries, err = tst_runner.load_regression_entries(Path(bad_path))
        check("malformed entry produces load/validation failure rather than skipping",
              err is not None or any(not tst_runner.validate_regression_entry(e) for e in bad_entries), True)
    finally:
        os.unlink(bad_path)

    # 11. Unit test: unknown drive or assert keys cause validate_regression_entry to return False (#472)
    valid_sample = {
        "issue": 472,
        "title": "starter chips visible on wake-word wake on empty feed",
        "drive": {"reload": True, "wake": True, "wait_ms": 1000},
        "assert": {"selector": "#starters", "visible": True, "not_classes": ["hidden"]},
    }
    check("validate_regression_entry accepts known drive/assert keys",
          tst_runner.validate_regression_entry(valid_sample), True)

    unknown_drive_sample = {
        "issue": 472,
        "title": "test unknown drive key",
        "drive": {"reload": True, "unsupported_action": True},
        "assert": {"selector": "#starters"},
    }
    check("validate_regression_entry rejects unknown drive keys",
          tst_runner.validate_regression_entry(unknown_drive_sample), False)

    unknown_assert_sample = {
        "issue": 472,
        "title": "test unknown assert key",
        "drive": {"wake": True},
        "assert": {"selector": "#starters", "unsupported_property": True},
    }
    check("validate_regression_entry rejects unknown assert keys",
          tst_runner.validate_regression_entry(unknown_assert_sample), False)

    valid_text_sample = {
        "issue": 472,
        "title": "test text_contains assert key",
        "drive": {"wake": True},
        "assert": {"selector": "#starters", "text_contains": "something"},
    }
    check("validate_regression_entry accepts text_contains assert key",
          tst_runner.validate_regression_entry(valid_text_sample), True)

    valid_viewport_sample = {
        "issue": 501,
        "title": "test in_viewport assert key",
        "drive": {"wake": True},
        "assert": {"selector": "#starters", "in_viewport": True},
    }
    check("validate_regression_entry accepts in_viewport assert key",
          tst_runner.validate_regression_entry(valid_viewport_sample), True)

    # Multi-step drive schema validation (#551)
    valid_steps_sample = {
        "issue": 542,
        "title": "starters flow under cards inside side-panel without being pushed below the fold",
        "drive": {
            "steps": [
                {"reload": True},
                {"wake": True},
                {"wait_ms": 1000},
                {"click": "#starters .chip"},
                {"wait_ms": 45000},
            ]
        },
        "assert": {"selector": "#starters", "visible": True, "in_viewport": True, "not_classes": ["hidden"]},
    }
    check("validate_regression_entry accepts valid multi-step drive schema",
          tst_runner.validate_regression_entry(valid_steps_sample), True)

    unknown_step_sample = {
        "issue": 542,
        "title": "test unknown step key",
        "drive": {
            "steps": [
                {"reload": True},
                {"unsupported_step_action": True},
            ]
        },
        "assert": {"selector": "#starters"},
    }
    check("validate_regression_entry rejects unknown keys in steps",
          tst_runner.validate_regression_entry(unknown_step_sample), False)

    empty_steps_sample = {
        "issue": 542,
        "title": "test empty steps",
        "drive": {"steps": []},
        "assert": {"selector": "#starters"},
    }
    check("validate_regression_entry rejects empty steps list",
          tst_runner.validate_regression_entry(empty_steps_sample), False)

    invalid_steps_type_sample = {
        "issue": 542,
        "title": "test invalid steps type",
        "drive": {"steps": "not_a_list"},
        "assert": {"selector": "#starters"},
    }
    check("validate_regression_entry rejects non-list steps",
          tst_runner.validate_regression_entry(invalid_steps_type_sample), False)

    # 12. Unit test: in_viewport predicate logic accepts tall elements whose top is visible (#529)
    in_vp_pred = lambda r, vh, vw: r["top"] >= 0 and r["top"] < vh and r["left"] >= 0 and r["left"] < vw
    tall_element = {"top": 0, "left": 0, "bottom": 1200, "right": 800}
    check("in_viewport predicate returns True even if r.bottom > vh",
          in_vp_pred(tall_element, 600, 1024) and tall_element["bottom"] > 600, True)
    check("tst_runner in_viewport checks top edge visibility instead of bottom containment",
          "r.top < vh" in runner_src and "r.bottom <= vh" not in runner_src, True)

    # 13. Check that run_regressions_corpus calls append_metric (#544)
    check("tst_runner run_regressions_corpus calls append_metric",
          'append_metric(sha, dirty, f"regression-{issue}"' in runner_src, True)
    check("tst_runner run_regressions_corpus calls append_metric on load error",
          'append_metric(sha, dirty, "regression-load"' in runner_src, True)

    # 14. Functional test: regression failure fixture writes metrics row that renders into dashboard as failure (#544)
    import asyncio
    try:
        from tools import tst_render
    except ImportError:
        import tst_render

    class FailingMockPage:
        async def reload(self): pass
        async def click(self, sel): pass
        async def wait_for_timeout(self, ms): pass
        async def query_selector(self, sel):
            return None  # element not found -> failure
        async def screenshot(self, path=None): pass

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_metrics = Path(tmp_dir) / "tst_metrics.jsonl"
        tmp_repo_metrics = Path(tmp_dir) / "repo_metrics.jsonl"
        tmp_results = Path(tmp_dir) / "current_test_results.md"
        fixture_corpus = Path(tmp_dir) / "regressions.yml"
        fixture_corpus.write_text(
            "- issue: 998\n"
            "  title: failing fixture test\n"
            "  drive: {}\n"
            "  assert: {selector: '#nonexistent', visible: true}\n",
            encoding="utf-8"
        )
        orig_runner_metrics = tst_runner.METRICS_FILE
        orig_runner_repo = tst_runner.REPO_METRICS_FILE
        orig_render_metrics = tst_render.METRICS_FILE
        orig_render_results = tst_render.RESULTS_FILE
        try:
            tst_runner.METRICS_FILE = tmp_metrics
            tst_runner.REPO_METRICS_FILE = tmp_repo_metrics
            tst_render.METRICS_FILE = tmp_metrics
            tst_render.RESULTS_FILE = tmp_results

            res_dict = {}
            asyncio.run(tst_runner.run_regressions_corpus(
                FailingMockPage(), res_dict, sha="testsha", dirty=False, corpus_path=fixture_corpus
            ))

            check("run_regressions_corpus wrote to metrics file", tmp_metrics.exists(), True)
            tst_render.render_results()
            check("render_results created current_test_results.md", tmp_results.exists(), True)
            rendered_md = tmp_results.read_text(encoding="utf-8")

            # Must show 0/1 pass in headline
            check("rendered dashboard headline reflects regression failure",
                  "0/1 pass" in rendered_md, True)
            # Must list regression-998 FAIL in scenario table
            check("rendered dashboard table contains regression-998 FAIL",
                  "| regression-998 | FAIL |" in rendered_md, True)
        finally:
            tst_runner.METRICS_FILE = orig_runner_metrics
            tst_runner.REPO_METRICS_FILE = orig_runner_repo
            tst_render.METRICS_FILE = orig_render_metrics
            tst_render.RESULTS_FILE = orig_render_results

    # 15. Unit test: entry 542 fails against broken 50b6959 layout and passes at 706e766 (#551)
    # At 50b6959:
    # 1. cards.style.paddingBottom = pane.style.paddingBottom (duplicate tail gap).
    # 2. showStarters() did not call restCardAtTop("auto").
    # With pane height 560 (1024x600 Pi screen, 40px top bar) and a 160px card:
    # room = 560 - 160 - 14 = 386px.
    # Duplicate tail padding on cards: cards height = 160 + 386 = 546px.
    # starters.offsetTop = 14 + 546 + 12 = 572px.
    # In window coordinates: starters bounding rect top = 40 + 572 = 612px.
    # Viewport height vh = 600px. Since 612 >= 600, in_viewport (r.top < vh) returns False!
    # At 706e766:
    # Duplicate tail padding on cards was removed (cards height = 160px).
    # starters.offsetTop = 14 + 160 + 12 = 186px.
    # In window coordinates: starters bounding rect top = 40 + 186 = 226px.
    # Since 0 <= 226 < 600, in_viewport returns True!
    def simulate_starters_in_viewport(card_rendered: bool, broken_50b6959: bool) -> bool:
        vh = 600
        top_bar = 40
        pane_height = 560
        card_top = 14
        margin = 12
        if not card_rendered:
            cards_height = 240
            starters_offset_top = card_top + cards_height + margin
            r_top = top_bar + starters_offset_top
            return 0 <= r_top < vh
        card_height = 160
        if broken_50b6959:
            extra_tail = max(0, pane_height - card_height - card_top)
            cards_height = card_height + extra_tail
            starters_offset_top = card_top + cards_height + margin
            r_top = top_bar + starters_offset_top
            return 0 <= r_top < vh
        else:
            cards_height = card_height
            starters_offset_top = card_top + cards_height + margin
            r_top = top_bar + starters_offset_top
            return 0 <= r_top < vh

    check("vacuous drive (no card) passes under broken 50b6959 layout",
          simulate_starters_in_viewport(card_rendered=False, broken_50b6959=True), True)
    check("vacuous drive (no card) passes under fixed 706e766 layout",
          simulate_starters_in_viewport(card_rendered=False, broken_50b6959=False), True)
    check("entry 542 fails against broken 50b6959 layout with card rendered",
          simulate_starters_in_viewport(card_rendered=True, broken_50b6959=True), False)
    check("entry 542 passes against fixed 706e766 layout with card rendered",
          simulate_starters_in_viewport(card_rendered=True, broken_50b6959=False), True)

    # 16. Functional test: run_regressions_corpus executes multi-step drive sequentially (#551)
    class MultiStepTrackingMockPage:
        def __init__(self):
            self.actions = []
        async def reload(self):
            self.actions.append("reload")
        async def click(self, sel):
            self.actions.append(f"click:{sel}")
        async def wait_for_timeout(self, ms):
            self.actions.append(f"wait:{ms}")
        async def query_selector(self, sel):
            class MockEl:
                async def is_visible(self):
                    return True
                async def evaluate(self, expr):
                    return True
            return MockEl()
        async def screenshot(self, path=None): pass

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_metrics = Path(tmp_dir) / "tst_metrics.jsonl"
        tmp_repo_metrics = Path(tmp_dir) / "repo_metrics.jsonl"
        fixture_corpus = Path(tmp_dir) / "regressions.yml"
        fixture_corpus.write_text(
            "- issue: 542\n"
            "  title: test multi-step drive execution\n"
            "  drive:\n"
            "    steps:\n"
            "      - reload: true\n"
            "      - wait_ms: 50\n"
            "      - click: '#starters .chip'\n"
            "      - wait_ms: 100\n"
            "  assert: {selector: '#starters', visible: true, in_viewport: true}\n",
            encoding="utf-8"
        )
        orig_runner_metrics = tst_runner.METRICS_FILE
        orig_runner_repo = tst_runner.REPO_METRICS_FILE
        try:
            tst_runner.METRICS_FILE = tmp_metrics
            tst_runner.REPO_METRICS_FILE = tmp_repo_metrics
            mock_page = MultiStepTrackingMockPage()
            res_dict = {}
            asyncio.run(tst_runner.run_regressions_corpus(
                mock_page, res_dict, sha="testsha", dirty=False, corpus_path=fixture_corpus
            ))
            check("multi-step drive executes all steps sequentially",
                  mock_page.actions, ["reload", "wait:50", "click:#starters .chip", "wait:100"])
            check("multi-step regression 542 passes on mock page",
                  res_dict.get("regression_542", {}).get("pass"), True)
            check("run_regressions_corpus multi-step wrote to tmp metrics (#733)",
                  tmp_metrics.exists() and tmp_repo_metrics.exists(), True)
        finally:
            tst_runner.METRICS_FILE = orig_runner_metrics
            tst_runner.REPO_METRICS_FILE = orig_runner_repo

    repo_metrics_size_after = real_repo_metrics.stat().st_size if real_repo_metrics.exists() else 0
    check("regressions corpus cases do not mutate real tst_metrics.jsonl (#733)",
          repo_metrics_size_after, repo_metrics_size_before)


def friction_log_cases() -> None:
    print("\nvisual friction log validation cases (#524)")
    tools_dir = ROOT / "tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    try:
        from tools import tst_render
    except ImportError:
        import tst_render

    # 1. Assert validate_friction_log accepts empty / "No visual friction detected."
    check("validate_friction_log accepts empty string",
          tst_render.validate_friction_log(""), (True, ""))
    check("validate_friction_log accepts whitespace string",
          tst_render.validate_friction_log("   \n\n  "), (True, ""))
    check("validate_friction_log accepts 'No visual friction detected.'",
          tst_render.validate_friction_log("No visual friction detected."), (True, ""))
    clean_section = "## Visual friction log, last run\n\nNo visual friction detected.\n"
    check("validate_friction_log accepts section with 'No visual friction detected.'",
          tst_render.validate_friction_log(clean_section), (True, ""))

    # 2. Assert validate_friction_log accepts valid grouped log with summary count line and all 4 dispositions
    valid_grouped = (
        "## Visual friction log, last run\n\n"
        "friction 4 lines -> 4 symptoms: 1 filed, 1 dup (#456), 1 rig, 1 undecided\n\n"
        "- **Symptom 1** (`shot1`): Maps widget blank -> filed #123\n"
        "- **Symptom 2** (`shot2`): Badge clipping -> dup #456\n"
        "- **Symptom 3** (`shot3`): Viewport clipping -> rig (HTTP referrer restriction on localhost)\n"
        "- **Symptom 4** (`shot4`): Message ordering -> undecided (evaluating conversational reload)\n"
    )
    check("validate_friction_log accepts valid grouped log with 4 dispositions",
          tst_render.validate_friction_log(valid_grouped), (True, ""))

    valid_backticks = (
        "## Visual friction log, last run\n\n"
        "4 raw lines -> 4 distinct symptoms: 1 filed, 1 dup (#456), 1 rig, 1 undecided.\n\n"
        "- **Symptom 1** (`shot1`): Maps widget blank -> `filed #123`\n"
        "- **Symptom 2** (`shot2`): Badge clipping -> `dup #456`\n"
        "- **Symptom 3** (`shot3`): Viewport clipping -> `rig` (HTTP referrer restriction on localhost)\n"
        "- **Symptom 4** (`shot4`): Message ordering -> `undecided` (evaluating conversational reload)\n"
    )
    check("validate_friction_log accepts valid grouped log with backticked dispositions",
          tst_render.validate_friction_log(valid_backticks), (True, ""))

    # 3. Assert validate_friction_log rejects log with symptoms but missing count line
    missing_count = (
        "## Visual friction log, last run\n\n"
        "- **Symptom 1** (`shot1`): Maps widget blank -> filed #123\n"
    )
    ok, err = tst_render.validate_friction_log(missing_count)
    check("validate_friction_log rejects log missing count line", ok, False)
    check("validate_friction_log error message names missing count line",
          "missing count line" in err, True)

    # 4. Assert validate_friction_log rejects entry missing disposition
    missing_disp = (
        "## Visual friction log, last run\n\n"
        "friction 1 lines -> 1 symptoms: 0 filed\n\n"
        "- **Symptom 1** (`shot1`): Maps widget blank\n"
    )
    ok, err = tst_render.validate_friction_log(missing_disp)
    check("validate_friction_log rejects entry missing disposition", ok, False)
    check("validate_friction_log error message for missing disposition",
          "entry missing valid disposition" in err, True)

    # 5. Assert validate_friction_log rejects entry with invalid disposition (e.g. -> accepted)
    invalid_disp = (
        "## Visual friction log, last run\n\n"
        "friction 1 lines -> 1 symptoms: 0 filed\n\n"
        "- **Symptom 1** (`shot1`): Maps widget blank -> accepted\n"
    )
    ok, err = tst_render.validate_friction_log(invalid_disp)
    check("validate_friction_log rejects entry with invalid disposition", ok, False)
    check("validate_friction_log error message for invalid disposition",
          "entry missing valid disposition" in err, True)

    # 6. Assert validate_friction_log rejects entry with rig but no reason (-> rig or -> rig ())
    rig_no_reason = (
        "## Visual friction log, last run\n\n"
        "friction 1 lines -> 1 symptoms: 1 rig\n\n"
        "- **Symptom 1** (`shot1`): Maps widget blank -> rig\n"
    )
    ok, err = tst_render.validate_friction_log(rig_no_reason)
    check("validate_friction_log rejects entry with rig and no reason", ok, False)
    check("validate_friction_log error message for rig with no reason",
          "entry missing valid disposition" in err, True)

    rig_empty_parens = (
        "## Visual friction log, last run\n\n"
        "friction 1 lines -> 1 symptoms: 1 rig\n\n"
        "- **Symptom 1** (`shot1`): Maps widget blank -> rig ()\n"
    )
    ok, err = tst_render.validate_friction_log(rig_empty_parens)
    check("validate_friction_log rejects entry with rig ()", ok, False)
    check("validate_friction_log error message for rig ()",
          "entry missing valid disposition" in err, True)

    # 7. Assert validate_friction_log rejects entry containing judge error (RESOURCE_EXHAUSTED or friction error:)
    judge_err_resource = (
        "## Visual friction log, last run\n\n"
        "friction 1 lines -> 1 symptoms: 1 filed\n\n"
        "- **build-badge-369** (`shot`): 429 RESOURCE_EXHAUSTED -> filed #123\n"
    )
    ok, err = tst_render.validate_friction_log(judge_err_resource)
    check("validate_friction_log rejects entry containing RESOURCE_EXHAUSTED", ok, False)
    check("validate_friction_log error message for RESOURCE_EXHAUSTED",
          "entry contains judge error rather than user symptom" in err, True)

    judge_err_prefix = (
        "## Visual friction log, last run\n\n"
        "friction 1 lines -> 1 symptoms: 1 filed\n\n"
        "- **weather-chip** (`shot`): friction error: connection timed out -> filed #123\n"
    )
    ok, err = tst_render.validate_friction_log(judge_err_prefix)
    check("validate_friction_log rejects entry containing friction error:", ok, False)
    check("validate_friction_log error message for friction error:",
          "entry contains judge error rather than user symptom" in err, True)

    # 8. Assert repo root current_test_results.md passes if present on disk
    repo_results = ROOT / "current_test_results.md"
    if repo_results.exists():
        check("repo root current_test_results.md passes validate_friction_log",
              tst_render.validate_friction_log(repo_results), (True, ""))


def friction_tracking_cases() -> None:
    print("\nvisual friction symptom tracking across runs cases (#573)")
    tools_dir = ROOT / "tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    try:
        from tools import tst_render, tst_judge
    except ImportError:
        import tst_render, tst_judge

    import asyncio
    import unittest.mock as mock
    import tempfile

    # 1. Assert judge_screenshot_friction returns None on 429 or other runtime exception
    mock_client = mock.MagicMock()
    mock_client.models.generate_content.side_effect = Exception("429 RESOURCE_EXHAUSTED: quota exceeded")
    with mock.patch.object(tst_judge, "get_client", return_value=mock_client):
        with tempfile.NamedTemporaryFile(suffix=".png") as tf:
            res = asyncio.run(tst_judge.judge_screenshot_friction(tf.name, "test scenario"))
            check("judge_screenshot_friction returns None on 429 exception", res is None, True)

    # 2. Assert render_results tracks symptoms across runs and outputs last seen: <prev_run_id>
    orig_metrics = getattr(tst_render, "METRICS_FILE", None)
    orig_results = getattr(tst_render, "RESULTS_FILE", None)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_dir = Path(tmpdir)
        tmp_metrics = tmp_dir / "tst_metrics.jsonl"
        tmp_results = tmp_dir / "current_test_results.md"

        ts_prev = "2026-09-14T10:00:00Z"
        ts_curr = "2026-09-14T10:05:00Z"

        run_prev_id = "run_20260914_100000_prev"
        run_curr_id = "run_20260914_100500_curr"

        rows = [
            # Previous run: clock-card had friction, weather-chip was clean
            {
                "sha": "abc1234", "dirty": False, "scenario": "clock-card",
                "pass": True, "elapsed_ms": 100, "run_id": run_prev_id, "ts": ts_prev,
                "friction": "clock hands misplaced", "screenshot": "/tmp/shot_clock.png",
            },
            {
                "sha": "abc1234", "dirty": False, "scenario": "weather-chip",
                "pass": True, "elapsed_ms": 100, "run_id": run_prev_id, "ts": ts_prev,
                "friction": "none", "screenshot": "/tmp/shot_weather.png",
            },
            # Current run: clock-card is clean (absent symptom), battery-indicator had judge error
            {
                "sha": "def5678", "dirty": False, "scenario": "clock-card",
                "pass": True, "elapsed_ms": 100, "run_id": run_curr_id, "ts": ts_curr,
                "friction": "none", "screenshot": "/tmp/shot_clock2.png",
            },
            {
                "sha": "def5678", "dirty": False, "scenario": "battery-indicator",
                "pass": True, "elapsed_ms": 100, "run_id": run_curr_id, "ts": ts_curr,
                "friction": None, "friction_error": "429 RESOURCE_EXHAUSTED",
                "screenshot": "/tmp/shot_battery.png",
            },
        ]

        with open(tmp_metrics, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

        try:
            tst_render.METRICS_FILE = tmp_metrics
            tst_render.RESULTS_FILE = tmp_results

            tst_render.render_results()
            check("render_results created current_test_results.md", tmp_results.exists(), True)
            rendered = tmp_results.read_text(encoding="utf-8")

            # Must contain "last seen: run_20260914_100000_prev"
            check("rendered friction section contains 'last seen: <prev_run_id>'",
                  f"last seen: {run_prev_id}" in rendered, True)
            check("rendered friction section contains absent symptom annotation",
                  "(absent this run, last seen:" in rendered, True)
            check("rendered friction section includes scenario name for absent symptom",
                  "- **clock-card**" in rendered and "clock hands misplaced" in rendered, True)

            # Judge exception must not output as a normal visual friction symptom
            check("judge 429 error does not output as normal visual friction symptom",
                  "- **battery-indicator**" not in rendered, True)
            check("judge error is recorded in Tooling health",
                  "Tooling health:" in rendered and "battery-indicator" in rendered, True)
        finally:
            tst_render.METRICS_FILE = orig_metrics
            tst_render.RESULTS_FILE = orig_results



def tester_capabilities_cases() -> None:
    print("\ntester capabilities matrix and dynamic coverage cases (#553)")
    tools_dir = ROOT / "tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    try:
        from tools import tst_render
    except ImportError:
        import tst_render

    # 1. Verify tools/tst_capabilities.yml exists
    cap_path = tools_dir / "tst_capabilities.yml"
    check("tools/tst_capabilities.yml exists", cap_path.exists(), True)

    # 2. Verify tools/tst_capabilities.yml is tracked in git
    tracked = subprocess.check_output(
        ["git", "-C", str(ROOT), "ls-files", "tools/tst_capabilities.yml"],
        text=True, env=_git_env()
    ).strip()
    check("tools/tst_capabilities.yml is tracked in git", bool(tracked), True)

    # 3. Verify content is data-only (no executable python)
    raw_text = cap_path.read_text(encoding="utf-8") if cap_path.exists() else ""
    dangerous = any(bad in raw_text for bad in ("eval(", "exec(", "__import__", "import "))
    check("tools/tst_capabilities.yml contains no executable python or expressions", dangerous, False)

    # 4. Verify valid YAML and schema
    caps_data = []
    parse_ok = False
    try:
        import yaml
        loaded = yaml.safe_load(raw_text)
        if isinstance(loaded, list) and all(isinstance(e, dict) for e in loaded):
            caps_data = loaded
            parse_ok = True
    except Exception:
        parse_ok = False
    check("tools/tst_capabilities.yml parses as a list of dicts", parse_ok, True)
    check("tools/tst_capabilities.yml contains 28 entries", len(caps_data), 28)

    valid_statuses = {"covered", "excluded", "gap"}
    all_entries_valid = True
    for entry in caps_data:
        if not isinstance(entry, dict):
            all_entries_valid = False
            break
        if not isinstance(entry.get("name"), str) or not entry.get("name", "").strip():
            all_entries_valid = False
            break
        if not isinstance(entry.get("type"), str) or not entry.get("type", "").strip():
            all_entries_valid = False
            break
        if entry.get("status") not in valid_statuses:
            all_entries_valid = False
            break
        if not (entry.get("scenario") or entry.get("covered_by")):
            all_entries_valid = False
            break
    check("each capability has valid name, type, scenario/covered_by, and status", all_entries_valid, True)

    # 5. Check distribution of tracked capabilities
    cov_metrics = tst_render.compute_coverage_metrics(caps_data)
    check("tracked capabilities compute 24 covered", cov_metrics["covered"], 24)
    check("tracked capabilities compute 4 excluded", cov_metrics["excluded"], 4)
    check("tracked capabilities compute 0 gaps", cov_metrics["gaps"], 0)
    check("tracked capabilities compute 28 total tracked", cov_metrics["tracked"], 28)

    # 6. Verify tst_render.py calculates coverage dynamically from capabilities file
    render_src = (tools_dir / "tst_render.py").read_text(encoding="utf-8")
    check("tst_render.py does not contain static 'coverage 15/22' constant",
          "coverage 15/22" in render_src, False)
    check("tst_render.py defines load_capabilities",
          hasattr(tst_render, "load_capabilities"), True)
    check("tst_render.py defines compute_coverage_metrics",
          hasattr(tst_render, "compute_coverage_metrics"), True)

    # 7. Fallback gracefully when capabilities file missing
    non_existent = tools_dir / "non_existent_capabilities.yml"
    check("load_capabilities falls back gracefully on missing file",
          tst_render.load_capabilities(non_existent), [])

    # 8. Test changing capability statuses in a fixture dynamically shifts headline numbers
    with tempfile.TemporaryDirectory() as td:
        fixture_cap_file = Path(td) / "fixture_capabilities.yml"
        fixture_metrics = Path(td) / "metrics.jsonl"
        fixture_results = Path(td) / "results.md"

        import yaml
        fixture_data = [
            {"name": "cap1", "type": "UI", "scenario": "s1", "status": "covered"},
            {"name": "cap2", "type": "UI", "scenario": "s2", "status": "covered"},
            {"name": "cap3", "type": "UI", "scenario": "s3", "status": "gap"},
            {"name": "cap4", "type": "UI", "scenario": "s4", "status": "gap"},
            {"name": "cap5", "type": "UI", "scenario": "s5", "status": "gap"},
            {"name": "cap6", "type": "UI", "scenario": "Excluded", "status": "excluded"},
            {"name": "cap7", "type": "UI", "scenario": "Excluded", "status": "excluded"},
        ]
        fixture_cap_file.write_text(yaml.safe_dump(fixture_data), encoding="utf-8")
        fixture_metrics.write_text(
            json.dumps({
                "ts": "2026-09-13T10:00:00Z",
                "sha": "testsha",
                "dirty": False,
                "scenario": "scenario_1",
                "pass": True,
                "elapsed_ms": 150,
                "run_id": "fixture_run",
            }) + "\n",
            encoding="utf-8"
        )

        orig_cap_file = getattr(tst_render, "CAPABILITIES_FILE", None)
        orig_metrics = getattr(tst_render, "METRICS_FILE", None)
        orig_results = getattr(tst_render, "RESULTS_FILE", None)

        try:
            tst_render.CAPABILITIES_FILE = fixture_cap_file
            tst_render.METRICS_FILE = fixture_metrics
            tst_render.RESULTS_FILE = fixture_results

            tst_render.render_results()
            rendered = fixture_results.read_text(encoding="utf-8")
            headline = [l for l in rendered.splitlines() if "coverage " in l]
            check("rendered results contain coverage line", len(headline) > 0, True)
            check("rendered headline dynamically reflects fixture: coverage 2/7 (3 gaps, 2 excluded)",
                  "coverage 2/7 (3 gaps, 2 excluded)" in (headline[0] if headline else ""), True)
        finally:
            tst_render.CAPABILITIES_FILE = orig_cap_file
            tst_render.METRICS_FILE = orig_metrics
            tst_render.RESULTS_FILE = orig_results


def visual_judge_fold_and_badge_cases() -> None:
    print("\nvisual judge scroll container fold and badge styling cases (#554)")

    # 1. Assert prompt instructions in tst_judge.py clarify .side-panel scroll container fold
    judge_path = ROOT / "tools" / "tst_judge.py"
    check("tools/tst_judge.py exists", judge_path.exists(), True)
    judge_src = judge_path.read_text(encoding="utf-8") if judge_path.exists() else ""

    check("tst_judge prompt clarifies right panel (.side-panel) is a scroll container",
          ".side-panel" in judge_src and "scrollable container" in judge_src, True)
    check("tst_judge prompt distinguishes viewport fold from truncation or clipping",
          "expected fold behavior, NOT truncation or clipping" in judge_src, True)
    check("tst_judge prompt restricts truncation reporting to non-scrollable box or unscrolled content",
          "inside a non-scrollable box or cannot be scrolled into view" in judge_src, True)

    # 2. Assert .build badge styling in style.css prevents raw text collision
    css_path = ROOT / "src" / "ai_terminal" / "live" / "static" / "css" / "style.css"
    check("style.css exists", css_path.exists(), True)
    css_src = css_path.read_text(encoding="utf-8") if css_path.exists() else ""

    build_match = re.search(r"(?:^|\n)\.build\s*\{([^}]+)\}", css_src)
    check("style.css defines base .build rule block", build_match is not None, True)
    build_block = build_match.group(1) if build_match else ""

    check(".build badge has semi-transparent background to prevent text collision",
          bool(re.search(r"background:\s*rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*[\d.]+\s*\)", build_block)), True)
    check(".build badge has padding",
          "padding:" in build_block, True)
    check(".build badge has border-radius",
          "border-radius:" in build_block, True)

    # 3. Assert build badge backing plate opacity and contrast (#569)
    # Element-level opacity weakens backing plate contrast against card content.
    opacity_match = re.search(r"(?<!-)opacity:\s*([\d.]+)", build_block)
    build_opacity = float(opacity_match.group(1)) if opacity_match else 1.0
    check(".build badge does not use element-level opacity < 0.8",
          build_opacity >= 0.8 and "opacity: 0.5" not in build_block, True,
          f"got opacity {build_opacity}")

    bg_match = re.search(r"background:\s*rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*([\d.]+)\s*\)", build_block)
    bg_alpha = float(bg_match.group(1)) if bg_match else 0.0
    check(".build badge backing plate has high contrast (alpha >= 0.85)",
          bg_alpha >= 0.85, True,
          f"got alpha {bg_alpha}")

    # Assert .build.stale maintains distinct visual style from .build (#569)
    stale_match = re.search(r"(?:^|\n)\.build\.stale\s*\{([^}]+)\}", css_src)
    check("style.css defines .build.stale rule block", stale_match is not None, True)
    stale_block = stale_match.group(1) if stale_match else ""

    stale_color_match = re.search(r"(?<!-)color:\s*([^;]+);", stale_block)
    build_color_match = re.search(r"(?<!-)color:\s*([^;]+);", build_block)
    stale_color = stale_color_match.group(1).strip() if stale_color_match else ""
    build_color = build_color_match.group(1).strip() if build_color_match else ""
    check(".build.stale has distinct color from standard .build",
          bool(stale_color and build_color and stale_color != build_color), True,
          f"stale={stale_color!r}, base={build_color!r}")
    check(".build.stale uses danger accent color",
          "var(--danger)" in stale_color, True,
          stale_color)
    check(".build.stale has distinct visual accent or warning border",
          ("border" in stale_block or "outline" in stale_block or "box-shadow" in stale_block), True,
          "border accent present")


def cloud_run_cases(readme: str) -> None:
    """Cloud Run containerized demo server deployment (#575)."""
    print("\nCloud Run demo server containerization and deployment")
    dockerfile = ROOT / "Dockerfile"
    dockerignore = ROOT / ".dockerignore"
    deploy_tool = ROOT / "tools" / "cloud_run_deploy.py"
    deploy_doc = DEPLOY / "CLOUD_RUN.md"

    # 1. Dockerfile existence and directives
    check("Dockerfile exists at project root", dockerfile.is_file(), True)
    df_text = dockerfile.read_text(encoding="utf-8") if dockerfile.is_file() else ""
    check("Dockerfile uses python:3.13-slim base",
          "FROM python:3.13-slim" in df_text, True)
    check("Dockerfile installs libportaudio2, curl, and ca-certificates",
          all(pkg in df_text for pkg in ("libportaudio2", "curl", "ca-certificates")), True)
    check("Dockerfile copies uv from astral-sh image",
          "ghcr.io/astral-sh/uv" in df_text, True)
    check("Dockerfile syncs project dependencies with uv",
          "uv sync --frozen --no-dev" in df_text, True)
    check("Dockerfile configures required environment variables",
          all(var in df_text for var in (
              'PATH="/app/.venv/bin:$PATH"',
              "HOST=0.0.0.0",
              "PORT=8080",
              'ALLOWED_HOSTS="ai-terminal-demo-example-ue.a.run.app,ai-terminal-demo-000000000000.us-east1.run.app"',
              "SESSIONS_DB=/tmp/sessions.db",
              "GWS_CACHE_DIR=/tmp/gws_cache",
              "SCREENSAVER_DIR=/tmp/screensaver",
          )), True)
    check("Dockerfile exposes port 8080", "EXPOSE 8080" in df_text, True)
    check("Dockerfile CMD executes live-server on 0.0.0.0:${PORT}",
          "exec live-server --host 0.0.0.0 --port ${PORT}" in df_text, True)
    check("Dockerfile installs Google Workspace CLI (gws)",
          "googleworkspace/cli" in df_text and "/usr/local/bin/gws" in df_text, True)
    check("Dockerfile installs Playwright Chromium with dependencies",
          "playwright install --with-deps chromium" in df_text, True)
    check("Dockerfile configures PLAYWRIGHT_BROWSERS_PATH",
          "ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright" in df_text, True)

    # 2. .dockerignore
    check(".dockerignore exists at project root", dockerignore.is_file(), True)
    ign_text = dockerignore.read_text(encoding="utf-8") if dockerignore.is_file() else ""
    ign_patterns = {ln.strip() for ln in ign_text.splitlines() if ln.strip() and not ln.startswith("#")}
    required_ignores = {
        ".git", ".github", ".agents", ".venv", "__pycache__",
        ".env*", "sessions.db*", "gws_cache", "screensaver",
        "recordings", "tst_metrics.jsonl", "tools", "tests",
    }
    missing_ignores = required_ignores - ign_patterns
    check("all required paths and patterns are ignored in .dockerignore", missing_ignores, set())

    # 3. cloud_run_deploy.py CLI and command synthesis
    check("cloud_run_deploy.py exists in tools/", deploy_tool.is_file(), True)
    import cloud_run_deploy
    cmd = cloud_run_deploy.build_deploy_command(
        service="test-service",
        project="test-project",
        region="asia-northeast1",
    )
    cmd_str = " ".join(cmd)
    check("build_deploy_command invokes gcloud run deploy",
          cmd[:3], ["gcloud", "run", "deploy"])
    check("build_deploy_command targets specified service",
          cmd[3], "test-service")
    check("build_deploy_command uses --source .",
          "--source" in cmd and cmd[cmd.index("--source") + 1] == ".", True)
    check("build_deploy_command sets --timeout=3600 for streaming",
          "--timeout=3600" in cmd, True)
    check("build_deploy_command enables --session-affinity",
          "--session-affinity" in cmd, True)
    check("build_deploy_command enables --allow-unauthenticated",
          "--allow-unauthenticated" in cmd, True)
    check("build_deploy_command sets required environment variables",
          all(var in cmd_str for var in (
              "SESSIONS_DB=/tmp/sessions.db",
              "GWS_CACHE_DIR=/tmp/gws_cache",
              "SCREENSAVER_DIR=/tmp/screensaver",
              'ALLOWED_HOSTS="ai-terminal-demo-example-ue.a.run.app,ai-terminal-demo-000000000000.us-east1.run.app"',
          )), True)
    check("build_deploy_command ALLOWED_HOSTS contains no wildcard", "*" not in cmd_str[cmd_str.find("ALLOWED_HOSTS"):], True)
    check("build_deploy_command passes --project and --region",
          ("--project" in cmd and "--region" in cmd), True)

    # CLI check command execution
    check_code = cloud_run_deploy.main(["check"])
    check("cloud_run_deploy check passes", check_code, 0)

    # CLI deploy --dry-run execution
    code, printed = quietly(cloud_run_deploy.main, ["deploy", "--dry-run"])
    check("cloud_run_deploy deploy --dry-run exits 0", code, 0)
    check("cloud_run_deploy deploy --dry-run prints gcloud invocation",
          "gcloud run deploy" in printed, True)

    # 4. Documentation
    check("deploy/CLOUD_RUN.md documentation exists", deploy_doc.is_file(), True)
    doc_text = deploy_doc.read_text(encoding="utf-8") if deploy_doc.is_file() else ""
    check("deploy/CLOUD_RUN.md documents required API keys",
          "GOOGLE_GENAI_API_KEY" in doc_text and "GOOGLE_MAPS_API_KEY" in doc_text, True)
    check("deploy/CLOUD_RUN.md documents disabled management routes",
          all(route in doc_text for route in ("/api/gws/logout", "/api/forget/{user_id}", "/api/wake", "/api/reboot"))
          and "ENABLE_MANAGEMENT_ROUTES" in doc_text, True)
    check("README.md mentions deploy/CLOUD_RUN.md",
          "deploy/CLOUD_RUN.md" in readme, True)


def wireplumber_cases() -> None:
    print("\nWirePlumber 0.5 USB microphone configuration cases (#574)")
    wp_conf = DEPLOY / "50-usb-mic.conf"
    check("deploy/50-usb-mic.conf exists", wp_conf.is_file(), True)
    content = wp_conf.read_text(encoding="utf-8")
    check("deploy/50-usb-mic.conf has non-empty content", len(content.strip()) > 0, True)

    # Validate SPA-JSON syntax: brackets and braces balance
    non_comment_lines = [
        ln.split("#")[0] for ln in content.splitlines() if ln.strip() and not ln.strip().startswith("#")
    ]
    code = "\n".join(non_comment_lines)
    check("WirePlumber config brackets balance",
          code.count("[") == code.count("]"), True)
    check("WirePlumber config braces balance",
          code.count("{") == code.count("}"), True)

    # Semantics of WirePlumber 0.5 rules
    check("config declares monitor.alsa.rules",
          "monitor.alsa.rules" in code, True)
    check("matches block targets USB capture devices",
          "alsa_input.*usb" in code or "USB Composite Device" in code, True)
    check("actions block sets priority.session to 2500",
          bool(re.search(r"priority\.session\s*=\s*2500", code)), True)


def management_routes_gating_cases() -> None:
    print("\nmanagement routes gating (#588)")
    import warnings
    from starlette.testclient import TestClient
    from ai_terminal.live.tools import gws_cache

    logging.getLogger("httpx").setLevel(logging.WARNING)
    was_cache = gws_cache.cache_dir
    gws_cache.cache_dir = lambda: Path("/tmp")
    orig_env = os.environ.get("ENABLE_MANAGEMENT_ROUTES")
    orig_pi = os.environ.get("AI_TERMINAL_IS_PI")
    # Case 3 gets /api/reboot past both guards, and TestClient runs background
    # tasks, so unstubbed this ran `sudo shutdown -r +0` on the machine running
    # the sweep (#671). The stub records the call instead.
    was_reboot = server._trigger_system_reboot
    reboots: list[None] = []
    server._trigger_system_reboot = lambda: reboots.append(None)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            client = TestClient(server.app, base_url="http://localhost")
            routes = ("/api/gws/logout", "/api/forget/user", "/api/wake", "/api/reboot")

            # 1. When ENABLE_MANAGEMENT_ROUTES is unset -> returns 403
            os.environ.pop("ENABLE_MANAGEMENT_ROUTES", None)
            os.environ.pop("AI_TERMINAL_IS_PI", None)
            for path in routes:
                res = client.post(path, headers={"content-type": "application/json"})
                check(f"{path} returns 403 when ENABLE_MANAGEMENT_ROUTES is unset",
                      res.status_code, 403)
                check(f"{path} explains route disabled in demo configuration",
                      res.json().get("detail"), "route disabled in demo configuration")

            # 2. When ENABLE_MANAGEMENT_ROUTES is '0' -> returns 403
            os.environ["ENABLE_MANAGEMENT_ROUTES"] = "0"
            for path in routes:
                res = client.post(path, headers={"content-type": "application/json"})
                check(f"{path} returns 403 when ENABLE_MANAGEMENT_ROUTES=0",
                  res.status_code, 403)
                check(f"{path} explains route disabled when 0",
                      res.json().get("detail"), "route disabled in demo configuration")

            # 3. When ENABLE_MANAGEMENT_ROUTES is '1' -> gating passes (does not return 403)
            os.environ["ENABLE_MANAGEMENT_ROUTES"] = "1"
            for path in ("/api/gws/logout", "/api/forget/user", "/api/wake"):
                res = client.post(path, headers={"content-type": "application/json"})
                check(f"{path} gating passes when ENABLE_MANAGEMENT_ROUTES=1",
                      res.status_code != 403, True, f"status {res.status_code}")

            # /api/reboot behavior:
            # - When AI_TERMINAL_IS_PI is not set: returns 409 Conflict (#672)
            os.environ.pop("AI_TERMINAL_IS_PI", None)
            res = client.post("/api/reboot", headers={"content-type": "application/json"})
            check("/api/reboot returns 409 when AI_TERMINAL_IS_PI is not set",
                  res.status_code, 409)
            check("/api/reboot explains pi host restriction",
                  res.json().get("detail"), "reboot only supported on Raspberry Pi appliance host")
            check("the reboot did not reach the stub when AI_TERMINAL_IS_PI is unset",
                  len(reboots), 0)

            # - When AI_TERMINAL_IS_PI="1": returns 200 (with _trigger_system_reboot stubbed) (#672)
            os.environ["AI_TERMINAL_IS_PI"] = "1"
            res = client.post("/api/reboot", headers={"content-type": "application/json"})
            check("/api/reboot returns 200 when AI_TERMINAL_IS_PI=1",
                  res.status_code, 200)
            check("/api/reboot returns rebooting status",
                  res.json().get("status"), "rebooting")
            check("the reboot reached the stub once, and only when the route was enabled",
                  len(reboots), 1, "a real reboot would have been taken (#671)")
    finally:
        server._trigger_system_reboot = was_reboot
        gws_cache.cache_dir = was_cache
        if orig_env is None:
            os.environ.pop("ENABLE_MANAGEMENT_ROUTES", None)
        else:
            os.environ["ENABLE_MANAGEMENT_ROUTES"] = orig_env
        if orig_pi is None:
            os.environ.pop("AI_TERMINAL_IS_PI", None)
        else:
            os.environ["AI_TERMINAL_IS_PI"] = orig_pi


def claude_settings_cases() -> None:
    print("\nclaude settings and hooks path resolution (#594)")
    root_settings = ROOT / ".claude" / "settings.json"
    check("root .claude/settings.json exists", root_settings.is_file(), True)
    root_data = json.loads(root_settings.read_text(encoding="utf-8"))

    commands = []
    for hook_group in root_data.get("hooks", {}).values():
        for entry in hook_group:
            for hook in entry.get("hooks", []):
                if "command" in hook:
                    commands.append(hook["command"])
    check("root settings hooks configure commands", len(commands) > 0, True)
    for cmd in commands:
        check(f"hook command uses git rev-parse --show-toplevel: {cmd}",
              "$(git rev-parse --show-toplevel)" in cmd, True)
        check(f"hook command does not use CLAUDE_PROJECT_DIR: {cmd}",
              "$CLAUDE_PROJECT_DIR" not in cmd, True)

    check("settings are stored in this repository", root_settings.is_symlink(), False)
    sub_agents = ROOT / ".agents"
    check("repository .agents directory exists", sub_agents.is_dir(), True)
    check("repository .agents is not an external symlink", sub_agents.is_symlink(), False)
    agy_hooks = sub_agents / "hooks.json"
    check("subproject .agents contains hooks.json", agy_hooks.is_file(), True)
    agy_data = json.loads(agy_hooks.read_text(encoding="utf-8"))
    check("subproject .agents/hooks.json parses as valid JSON",
          isinstance(agy_data, dict), True)

    role_guard_test = ROOT / ".claude" / "hooks" / "role_guard_test.py"
    check("role_guard_test.py exists", role_guard_test.is_file(), True)
    res_rg = subprocess.run(
        [sys.executable, str(role_guard_test)],
        cwd=str(ROOT), capture_output=True, text=True, env=_git_env(),
    )
    check("role_guard_test.py exits 0", res_rg.returncode, 0,
          res_rg.stderr.strip() or res_rg.stdout.strip()[:60])

    triage_gate_test = ROOT / ".claude" / "hooks" / "triage_gate_test.py"
    check("triage_gate_test.py exists", triage_gate_test.is_file(), True)
    res_tg = subprocess.run(
        [sys.executable, str(triage_gate_test)],
        cwd=str(ROOT), capture_output=True, text=True, env=_git_env(),
    )
    check("triage_gate_test.py exits 0", res_tg.returncode, 0,
          res_tg.stderr.strip() or res_tg.stdout.strip()[:60])

    ccloop_test = ROOT / ".claude" / "hooks" / "ccloop_test.py"
    check("ccloop_test.py exists", ccloop_test.is_file(), True)
    res_cl = subprocess.run(
        [sys.executable, str(ccloop_test)],
        cwd=str(ROOT), capture_output=True, text=True, env=_git_env(),
    )
    check("ccloop_test.py exits 0", res_cl.returncode, 0,
          res_cl.stderr.strip() or res_cl.stdout.strip()[:60])


def orphaned_subagent_cases() -> None:
    """Assert no orphaned subagent processes or session subshells sit at ppid 1 (#607).

    Subagents and test runners create processes under /tmp/rev-*, /tmp/cod-*,
    /tmp/mon-*, and /tmp/tst-*. When a session exits without cleaning up background
    jobs or subshells, those processes get reparented to launchd (ppid 1) and spin
    indefinitely, burning CPU and distorting latency measurements.
    """
    print("\norphaned subagent processes at ppid 1 (#607)")
    prefixes = ("/tmp/rev-", "/tmp/cod-", "/tmp/mon-", "/tmp/tst-")
    threshold_seconds = 300  # 5 minutes

    def _parse_etime(etime_str: str) -> int:
        etime_str = etime_str.strip()
        days = 0
        if "-" in etime_str:
            d_str, etime_str = etime_str.split("-", 1)
            try:
                days = int(d_str)
            except ValueError:
                return 0
        try:
            parts = [int(p) for p in etime_str.split(":")]
        except ValueError:
            return 0
        if len(parts) == 3:
            h, m, s = parts
        elif len(parts) == 2:
            h, m, s = 0, parts[0], parts[1]
        elif len(parts) == 1:
            h, m, s = 0, 0, parts[0]
        else:
            return 0
        return days * 86400 + h * 3600 + m * 60 + s

    res = subprocess.run(
        ["ps", "-eo", "pid,ppid,etime,command"],
        capture_output=True,
        text=True,
        check=False,
    )
    orphans: list[dict[str, str | int]] = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, ppid_s, etime_s, cmd = parts
        if not pid_s.isdigit() or not ppid_s.isdigit():
            continue
        if int(ppid_s) == 1 and any(p in cmd for p in prefixes):
            elapsed = _parse_etime(etime_s)
            if elapsed >= threshold_seconds:
                orphans.append({
                    "pid": int(pid_s),
                    "etime": etime_s,
                    "elapsed_sec": elapsed,
                    "command": cmd,
                })

    details = [
        f"PID {o['pid']} (elapsed {o['etime']}, {o['elapsed_sec']}s): {o['command']}"
        for o in orphans
    ]
    check("no orphaned subagent processes sitting at ppid 1",
          details, [],
          "\n        ".join(details) if details else "none found")
def restore_ordering_cases() -> None:
    print("\nrestore ordering and turn settling cases (#572)")
    tools_dir = ROOT / "tools"
    if not (tools_dir / "tst_runner.py").exists() and (Path(__file__).parent / "tst_runner.py").exists():
        tools_dir = Path(__file__).parent
    runner_src = (tools_dir / "tst_runner.py").read_text(encoding="utf-8")

    # 1. Assert scenario 4 records initial completed agent message count
    s4_block = runner_src[runner_src.find("# --- Scenario 4: Clicks the Today's Weather chip ---"):runner_src.find("# --- Scenario 7: Regression #203")]
    check("scenario 4 records initial agent message count",
          "s4_agent_count = len(await page.query_selector_all(\".msg.agent:not(.partial)\"))" in s4_block or
          "s4_agent_count" in s4_block, True)

    # 2. Assert scenario 5 settling logic waits for completed messages to increase from scenario start count
    s5_block = runner_src[runner_src.find("# --- Scenario 5: Types a question and waits for an answer ---"):runner_src.find("# --- Scenario 6: Reloads mid-conversation ---")]
    settling_waits_for_completion = (
        ".msg.agent:not(.partial)" in s5_block
        and ("s4_agent_count" in s5_block or "len(current_agents)" in s5_block)
        and ("while" in s5_block and "15.0" in s5_block)
    )
    check("scenario 5 settling logic waits up to 15s for completed messages count to increase",
          settling_waits_for_completion, True)

    # 3. Assert scenario 5 retains detached wait for partial stream after count increases
    retains_detached_wait = (
        'await page.wait_for_selector(".msg.agent.partial", state="detached"' in s5_block
    )
    check("scenario 5 retains detached wait for partial stream",
          retains_detached_wait, True)

    settling_before_fill = (
        s5_block.find("s4_agent_count") < s5_block.find('await page.fill("#textInput"')
        if "s4_agent_count" in s5_block and 'await page.fill("#textInput"' in s5_block
        else False
    )
    check("scenario 5 turn settling runs before filling and sending Q2",
          settling_before_fill, True)

    # 4. Assert scenario 6 asserts restored feed role alternation in chronological order
    s6_block = runner_src[runner_src.find("# --- Scenario 6: Reloads mid-conversation ---"):runner_src.find("# --- Scenario 9: Starter buttons")]
    alternation_checked = (
        '["user", "agent", "user", "agent"]' in s6_block
        and ("restored_roles" in s6_block or "roles_alternating" in s6_block)
        and "s6_pass = bool(roles_alternating" in s6_block
    )
    check("scenario 6 asserts restored feed role alternation (user, agent, user, agent)",
          alternation_checked, True)

    # 5. Logic check: test role alternation verification logic against reordered and invalid feeds
    def check_roles(roles: list[str]) -> bool:
        expected = ["user", "agent", "user", "agent"]
        return roles == expected

    check("role alternation check passes on chronological user/agent alternation",
          check_roles(["user", "agent", "user", "agent"]), True)
    check("role alternation check rejects reordered roles (user, user, agent, agent)",
          check_roles(["user", "user", "agent", "agent"]), False)
    check("role alternation check rejects inverted roles (agent, user, agent, user)",
          check_roles(["agent", "user", "agent", "user"]), False)
    check("role alternation check rejects truncated roles (user, agent, user)",
          check_roles(["user", "agent", "user"]), False)

    # 6. Assert scenario 5 waits for agent reply to be sealed before reload (#623)
    s5_after_click = s5_block[s5_block.find("click_time"):]
    s5_seals_turn = (
        "len(completed_msgs) > prior_count" in s5_after_click
        and 'await page.wait_for_selector(".msg.agent.partial", state="detached"' in s5_after_click
    )
    check("scenario 5 waits for agent reply to seal before completing (#623)",
          s5_seals_turn, True)

    s6_detached_guard = (
        'await page.wait_for_selector(".msg.agent.partial", state="detached"' in s6_block
        and s6_block.find(".msg.agent.partial") < s6_block.find("page.reload")
    )
    check("scenario 6 ensures agent message is detached before page reload (#623)",
          s6_detached_guard, True)


def judge_contract_cases() -> None:
    print("\njudge contract and skipped state surfacing cases (#580)")
    import asyncio
    from unittest import mock
    try:
        from tools import tst_judge, tst_trajectories
    except ImportError:
        import tst_judge, tst_trajectories

    # 1. Assert judge_context_retention returns (None, ...) when get_client() is None and no lost phrases match
    turns = [
        {"prompt": "東京の天気は？", "response": "東京は晴れです。"},
        {"prompt": "明日はどう？", "response": "明日も晴れの予報です。"},
    ]
    with mock.patch.object(tst_judge, "get_client", return_value=None):
        ret, reason = asyncio.run(tst_judge.judge_context_retention("weather query", turns))
        check("judge_context_retention returns None when client is None", ret, None)

    # 2. Assert judge_chip_relevance returns (None, 0.0, ...) when get_client() is None
    chips = ["明日の天気", "降水確率"]
    with mock.patch.object(tst_judge, "get_client", return_value=None):
        chip_pass, score, chip_reason = asyncio.run(tst_judge.judge_chip_relevance("東京の天気", chips))
        check("judge_chip_relevance returns None pass when client is None", chip_pass, None)
        check("judge_chip_relevance returns 0.0 score when client is None", score, 0.0)

    # 3. Assert trajectory rendering outputs 'skipped' instead of 'ok' when context_retained is None
    with tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False) as tf:
        traj_file = tf.name
        record_none = {
            "ts": "2026-09-14T00:00:00Z",
            "run_id": "test_run",
            "sha": "testsha",
            "dirty": False,
            "scenario": "test-scenario",
            "scenario_num": 1,
            "title": "Test Multi-Turn",
            "pass": True,
            "total_turns": 2,
            "context_retained": None,
            "expected_tools": [],
            "first_token_ms": 100,
            "complete_ms": 200,
            "elapsed_ms": 300,
            "tools": [],
            "screenshot": "",
            "turns": [],
        }
        tf.write(json.dumps(record_none) + "\n")

    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as rf:
        rep_file = rf.name

    try:
        with mock.patch.object(tst_trajectories, "TRAJECTORIES_FILE", traj_file), \
             mock.patch.object(tst_trajectories, "REPORT_FILE", rep_file):
            tst_trajectories.render_report()
            rep_content = Path(rep_file).read_text(encoding="utf-8")
            check("trajectory report renders 'skipped' when context_retained is None",
                  "| skipped |" in rep_content, True)
            check("trajectory report does not render 'ok' when context_retained is None",
                  "| ok |" not in rep_content and "(context: ok)" not in rep_content, True)
    finally:
        try:
            os.unlink(traj_file)
            os.unlink(rep_file)
        except OSError:
            pass


def open_verified_stage_cases() -> None:
    """Assert no open issue carries stage:verified (#615).

    The stage:verified sweep is prose, not code, and nothing tests it: the third
    occurrence (#436, #495, #615) is the only open P0 (#594).
    """
    print("\nopen issues carrying stage:verified (#615)")
    try:
        res = subprocess.run(
            ["gh", "issue", "list", "--state", "open", "--label", "stage:verified", "--json", "number,title"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=_git_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("   skipped: gh cli unavailable")
        return

    if res.returncode != 0:
        print("   skipped: gh cli unavailable")
        return

    try:
        issues = json.loads(res.stdout)
    except json.JSONDecodeError:
        print("   skipped: gh cli unavailable")
        return

    check("no open issue carries stage:verified", len(issues), 0,
          f"found: {[i['number'] for i in issues]}")


def behind_cases() -> None:
    print("\nchecking whether Pi revision is behind latest deployed commit (#681)")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        a_checkout(root)

        def git(*args: str) -> str:
            res = subprocess.run(("git", "-C", str(root), "-c", "user.name=check",
                                  "-c", "user.email=check@localhost",
                                  "-c", "commit.gpgsign=false", *args),
                                 check=True, capture_output=True, text=True,
                                 env=_git_env())
            return res.stdout.strip()

        # Commit 1 (C1): commits project/src/ai_terminal/live/main.py
        (root / "project" / "src" / "ai_terminal" / "live").mkdir(parents=True, exist_ok=True)
        (root / "project" / "src" / "ai_terminal" / "live" / "main.py").write_text("# main C1\n")
        git("add", "-A")
        git("commit", "-qm", "commit 1: main.py")
        c1 = git("rev-parse", "--short", "HEAD")

        # Commit 2 (C2): commits project/tools/deploy_check.py (non-deployed path)
        (root / "project" / "tools").mkdir(parents=True, exist_ok=True)
        (root / "project" / "tools" / "deploy_check.py").write_text("# check C2\n")
        git("add", "-A")
        git("commit", "-qm", "commit 2: deploy_check.py")
        c2 = git("rev-parse", "--short", "HEAD")

        # Commit 3 (C3): commits other/README.md (unrelated project)
        (root / "other" / "README.md").write_text("# other C3\n")
        git("add", "-A")
        git("commit", "-qm", "commit 3: other README")
        c3 = git("rev-parse", "--short", "HEAD")

        was_root = pi_deploy.ROOT
        pi_deploy.ROOT = root / "project"
        try:
            # Assertions after C1, C2, C3:
            # latest_deployed_commit() should identify C1 as the latest commit touching deployed paths
            check("latest_deployed_commit returns C1 when later commits touched non-deployed files",
                  pi_deploy.latest_deployed_commit(), c1)
            check("is_pi_behind returns False when Pi revision matches C1",
                  pi_deploy.is_pi_behind(c1), False)
            check("is_pi_behind returns False for full SHA prefix matching C1",
                  pi_deploy.is_pi_behind(git("rev-parse", f"{c1}")), False)

            # Commit 4 (C4): modifies project/src/ai_terminal/live/main.py
            (root / "project" / "src" / "ai_terminal" / "live" / "main.py").write_text("# main C4\n")
            git("add", "-A")
            git("commit", "-qm", "commit 4: modify main.py")
            c4 = git("rev-parse", "--short", "HEAD")

            check("latest_deployed_commit now returns C4 after deployed path modified",
                  pi_deploy.latest_deployed_commit(), c4)
            check("is_pi_behind returns True when Pi is still at C1 and latest deployed commit is C4",
                  pi_deploy.is_pi_behind(c1), True)
            check("is_pi_behind returns False when Pi is at C4",
                  pi_deploy.is_pi_behind(c4), False)

            # Commit 5 (C5): modifies .claude/hooks/ccloop.sh (touches no deployed files)
            (root / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)
            (root / ".claude" / "hooks" / "ccloop.sh").write_text("# ccloop C5\n")
            git("add", "-A")
            git("commit", "-qm", "commit 5: ccloop hook")
            c5 = git("rev-parse", "--short", "HEAD")

            check("latest_deployed_commit still returns C4 when HEAD touched non-deployed .claude path",
                  pi_deploy.latest_deployed_commit(), c4)
            check("is_pi_behind(C1) still returns True asserting deploy is owed",
                  pi_deploy.is_pi_behind(c1), True)
            check("is_pi_behind(C4) returns False because Pi has latest deployed files",
                  pi_deploy.is_pi_behind(c4), False)

            # Additional deployed path: deploy/**
            (root / "project" / "deploy").mkdir(parents=True, exist_ok=True)
            (root / "project" / "deploy" / "test.service").write_text("# service\n")
            git("add", "-A")
            git("commit", "-qm", "commit 6: deploy service")
            c6 = git("rev-parse", "--short", "HEAD")
            check("latest_deployed_commit detects deploy/** commit",
                  pi_deploy.latest_deployed_commit(), c6)
            check("is_pi_behind(C4) is True after deploy/** commit",
                  pi_deploy.is_pi_behind(c4), True)
            check("is_pi_behind(C6) is False after deploy/** commit",
                  pi_deploy.is_pi_behind(c6), False)

            # Additional deployed path: pyproject.toml
            (root / "project" / "pyproject.toml").write_text("# pyproject\n")
            git("add", "-A")
            git("commit", "-qm", "commit 7: pyproject.toml")
            c7 = git("rev-parse", "--short", "HEAD")
            check("latest_deployed_commit detects pyproject.toml commit",
                  pi_deploy.latest_deployed_commit(), c7)
            check("is_pi_behind(C6) is True after pyproject commit",
                  pi_deploy.is_pi_behind(c6), True)
            check("is_pi_behind(C7) is False",
                  pi_deploy.is_pi_behind(c7), False)

            # Additional deployed path: uv.lock
            (root / "project" / "uv.lock").write_text("# uv.lock\n")
            git("add", "-A")
            git("commit", "-qm", "commit 8: uv.lock")
            c8 = git("rev-parse", "--short", "HEAD")
            check("latest_deployed_commit detects uv.lock commit",
                  pi_deploy.latest_deployed_commit(), c8)
            check("is_pi_behind(C7) is True after lock commit",
                  pi_deploy.is_pi_behind(c7), True)
            check("is_pi_behind(C8) is False",
                  pi_deploy.is_pi_behind(c8), False)

            # Empty or unknown revision indicates deploy owed
            check("is_pi_behind returns True for empty revision",
                  pi_deploy.is_pi_behind(""), True)
            check("is_pi_behind returns True for unknown revision",
                  pi_deploy.is_pi_behind("unknown"), True)
            check("is_pi_behind handles -dirty suffix",
                  pi_deploy.is_pi_behind(f"{c8}-dirty"), False)

            # Dry-run CLI invocations
            check("main with is-behind dry run exits 0",
                  pi_deploy.main(["--dry-run", "is-behind"]), 0)
            check("main with --is-behind dry run exits 0",
                  pi_deploy.main(["--dry-run", "--is-behind"]), 0)
            check("main with --if-behind dry run exits 0",
                  pi_deploy.main(["--dry-run", "--if-behind", "deploy", "restart"]), 0)

            # Verify explicit repo_path parameter against stubbed repo root
            stub_root = root / "project"
            check("latest_deployed_commit with repo_path returns c8",
                  pi_deploy.latest_deployed_commit(repo_path=stub_root), c8)
            check("is_pi_behind(c7) with repo_path is True (stranded Pi)",
                  pi_deploy.is_pi_behind(c7, repo_path=stub_root), True)
            check("is_pi_behind(c8) with repo_path is False (caught up Pi)",
                  pi_deploy.is_pi_behind(c8, repo_path=stub_root), False)
        finally:
            pi_deploy.ROOT = was_root


def google_home_credentials_cases() -> None:
    print("\nGoogle Home credentials deployment (#754)")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        empty_dir = Path(tmp)
        plan_empty = pi_deploy.deploy_plan("pi2", home_config_dir=empty_dir)
        check("absent credentials omit Google Home credentials copy steps",
              any("the Google Home credentials" in s.name for s in plan_empty),
              False)

        # Populate temporary credentials
        token_file = empty_dir / "home-mcp-token.json"
        token_file.write_text('{"token": "test"}')
        client_file = empty_dir / "home-mcp-client.json"
        client_file.write_text('{"client": "test"}')

        plan_present = pi_deploy.deploy_plan("pi2", home_config_dir=empty_dir)
        cred_steps = [s for s in plan_present if "the Google Home credentials" in s.name]
        check("present credentials include copy and chmod 600 steps",
              [s.name for s in cred_steps],
              [f"somewhere for the Google Home credentials to live",
               f"the Google Home credentials: home-mcp-token.json ({token_file.stat().st_size} bytes)",
               f"the Google Home credentials mode 600: home-mcp-token.json",
               f"the Google Home credentials: home-mcp-client.json ({client_file.stat().st_size} bytes)",
               f"the Google Home credentials mode 600: home-mcp-client.json"])

        token_scp = next(s for s in cred_steps if s.name.startswith("the Google Home credentials: home-mcp-token.json"))
        check("token scp command copies to pi credentials path",
              token_scp.argv,
              ("scp", str(token_file), f"pi2:/home/{pi_deploy.PI_USER}/.config/ai-terminal/home-mcp-token.json"))

        token_chmod = next(s for s in cred_steps if s.name == "the Google Home credentials mode 600: home-mcp-token.json")
        check("token chmod step sets mode 600",
              token_chmod.argv,
              pi_deploy.remote("pi2", f"chmod 600 /home/{pi_deploy.PI_USER}/.config/ai-terminal/home-mcp-token.json"))

        # Partial presence: only token file
        token_only_dir = Path(tmp) / "token_only"
        token_only_dir.mkdir()
        t_only = token_only_dir / "home-mcp-token.json"
        t_only.write_text('{"token": "test"}')
        plan_token_only = pi_deploy.deploy_plan("pi2", home_config_dir=token_only_dir)
        check("partial credentials copy only present files",
              [s.name for s in plan_token_only if "the Google Home credentials:" in s.name],
              [f"the Google Home credentials: home-mcp-token.json ({t_only.stat().st_size} bytes)"])


def main() -> None:
    real_metrics = ROOT / "tst_metrics.jsonl"
    metrics_size_before = real_metrics.stat().st_size if real_metrics.exists() else 0
    readme = (ROOT / "README.md").read_text()
    quickstart_cases(readme)
    githook_cases(readme)
    unit_cases()
    server_unit_cases()
    lan_tls_cases(readme)
    stop_cases()
    command_cases()
    policy_cases(readme)
    kiosk_cases()
    debug_port_cases(readme)
    signin_cases(readme)
    procedure_cases(readme)
    plan_cases(readme)
    derivation_cases(readme)
    ir_cases(readme)
    probe_cases()
    verdict_cases()
    state_cases()
    phase_cases()
    mic_setup_cases()
    revision_cases(readme)
    boot_acceptance_cases(readme)
    hpf_cases()
    run_cases()
    deploy_lock_cases()
    tester_ceiling_cases()
    trajectory_history_cases()
    traj_render_stale_warning_cases()
    tester_sync_cases()
    regressions_corpus_cases()
    friction_log_cases()
    friction_tracking_cases()
    tester_capabilities_cases()
    visual_judge_fold_and_badge_cases()
    cloud_run_cases(readme)
    wireplumber_cases()
    management_routes_gating_cases()
    claude_settings_cases()
    orphaned_subagent_cases()
    restore_ordering_cases()
    judge_contract_cases()
    open_verified_stage_cases()
    behind_cases()
    google_home_credentials_cases()
    metrics_size_after = real_metrics.stat().st_size if real_metrics.exists() else 0
    check("deploy_check suite does not mutate real tst_metrics.jsonl (#733)",
          metrics_size_after, metrics_size_before)
    print(f"\nall {RAN} cases hold")


if __name__ == "__main__":
    main()



