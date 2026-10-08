"""Install the terminal on the Pi, and say what is wrong when it is not.

Until this existed the Pi was installed by reading README prose and pasting
nine shell blocks out of three sections, and nothing anywhere reported whether
the result was correct. It was not: on 2026-09-09 six prerequisites were unmet
on pi2 and every one of them failed silently — no `deploy/` in the copied tree,
no model under the path the unit names, no wake unit installed at all, no
managed-policy directory, `Linger=no`, and a microphone bias that DAPM had
powered down with nothing wired to put it back.

Four phases, in the order they have to happen:

    uv run python tools/pi_deploy.py                 # all four, in order
    uv run python tools/pi_deploy.py check           # read-only
    uv run python tools/pi_deploy.py deploy --dry-run
    uv run python tools/pi_deploy.py --host 192.0.2.10

`check` opens an ssh connection and runs nothing that writes. That is not a
comment, it is asserted: `mutating` reads each command back and
`tools/deploy_check.py` fails if a write ever appears in the `check` plan. A
"check" that quietly repairs and then reports green on a Pi it has just changed
is worse than no check, because the next person believes it.

**Not named `*_check.py`,** although it checks. `tools/check_all.py` globs that
suffix, so the name would enrol a script that ssh's to a host on the LAN and
restarts the kiosk into the offline sweep — a sweep that cannot run on a plane
and blanks the panel in the middle of a review.

**Python rather than shell,** unlike `mic_setup.sh` next door. Those are
straight-line device pokes. This parses `dpkg-query`, `loginctl show-user`,
`aplay -L`, `arecord -l` and `systemctl --user`, folds the results into a table
with a verdict per row, and has to be assertable from a laptop with the Pi
switched off. That is a program.

**Every constant that also lives in a deploy artifact is read out of it** —
the directory the rsync writes, the uv the unit invokes, the model path, the
origin the policy grants. The one deliberate exception is the rsync exclude
list, which is spelled here *and* in the README so that `deploy_check` can
compare them: excluding `sessions.db*` in one place and not the other is the
kind of divergence that costs the household its conversations, and a single
source would make the comparison vacuous.

What this cannot do is hear. The wake word is verified by standing at the panel
and saying the phrase, so the last thing printed is that command rather than a
green tick.
"""

from __future__ import annotations

import argparse
import os
import hashlib
import re
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"

SERVER_UNIT = DEPLOY / "ai-terminal.service"
WAKE_UNIT = DEPLOY / "ai-terminal-wake.service"
KIOSK_UNIT = DEPLOY / "ai-terminal-kiosk.service"
TUNNEL_UNIT = DEPLOY / "ai-terminal-tunnel.service"
POLICY = DEPLOY / "ai-terminal-chromium-policy.json"

_GIT_SCRUB = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
              "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")


def _git_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_SCRUB}


JOURNAL = DEPLOY / "50-ai-terminal-journal.conf"
JOURNAL_DIR = "/etc/systemd/journald.conf.d"

KANSHI_CONFIG = DEPLOY / "kanshi-config"
LABWC_CONFIG = DEPLOY / "labwc-rc.xml"
TOUCH_RULES = DEPLOY / "99-touch-calibration.rules"
WIREPLUMBER_CONFIG = DEPLOY / "50-usb-mic.conf"


# --------------------------------------------------------------------------
# reading the deploy artifacts
#
# `deploy_check.py` imports these rather than keeping a second parser. The
# installer is the thing that has to agree with the units; the suite checks
# that it does, and it cannot check it against a copy of itself.
# --------------------------------------------------------------------------

def unit(path: Path) -> dict[str, list[tuple[str, str]]]:
    """A systemd unit, as sections of key/value pairs in the order written.

    Not `configparser`: duplicate keys are meaningful here — two
    `ExecStartPre=` lines are two commands, and an empty `ExecStart=` is an
    erasure — and a value continues onto the next line after a trailing
    backslash, which configparser reads as an indented continuation and joins
    with a newline instead of a space.
    """
    sections: dict[str, list[tuple[str, str]]] = {}
    current: list[tuple[str, str]] = []
    held = ""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if held:
            line, held = f"{held} {line}", ""
        if line.endswith("\\"):
            held = line[:-1].strip()
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], [])
            continue
        key, _, value = line.partition("=")
        current.append((key.strip(), value.strip()))
    return sections


def every(sections, section: str, key: str) -> list[str]:
    return [v for k, v in sections.get(section, []) if k == key]


def one(sections, section: str, key: str) -> str | None:
    found = every(sections, section, key)
    return found[0] if len(found) == 1 else None


def url_in(exec_start: str) -> str:
    """The one http URL on a unit's command line."""
    found = re.findall(r"https?://\S+", exec_start)
    return found[0] if len(found) == 1 else ""


def origin(url: str) -> str:
    split = urlsplit(url)
    return f"{split.scheme}://{split.netloc}" if split.scheme else ""


def flag(exec_start: str, name: str) -> str:
    """The value after `--name` on a command line, or an empty string."""
    argv = exec_start.split()
    for i, token in enumerate(argv):
        if token == name and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith(f"{name}="):
            return token.split("=", 1)[1]
    return ""


def _need(value: str, what: str) -> str:
    """A path the unit has to supply, or a refusal to guess at one.

    Every path this installer touches on the Pi comes out of
    `ai-terminal-wake.service`, and a default here would be the installer
    quietly disagreeing with the unit about where the model lives — which is
    the five-second restart loop this whole issue is about, moved one file
    over. Better to stop.
    """
    if not value:
        raise SystemExit(f"{WAKE_UNIT.name} has no {what}, and the installer "
                         "takes every path on the Pi from it")
    return value


_WAKE = unit(WAKE_UNIT)
_WAKE_EXEC = _need(one(_WAKE, "Service", "ExecStart") or "", "ExecStart=")

# The directory the rsync writes and the unit runs out of. One string, in the
# unit, and everything below that needs a path on the Pi is relative to it.
PI_DIR = _need(one(_WAKE, "Service", "WorkingDirectory") or "",
               "WorkingDirectory=")
PI_USER = PurePosixPath(PI_DIR).parent.name
LOCK_FILE = f"{PI_DIR}/.deploy.lock"
LOCK_MAX_AGE_S = 600

# `uv` is not on PATH in a non-interactive ssh shell, which is why the unit
# spells it absolutely. Taking it from there means the installer and the unit
# cannot disagree about which uv built the environment the unit runs in.
UV = _need(_WAKE_EXEC.split()[0], "a command in its ExecStart=")

# The model path is parsed, never spelled: `scp`ing the unit without a fresh
# tree under it gives Restart=always looping on a missing file every five
# seconds forever, with the only evidence in a journal nobody is reading.
MODEL = _need(flag(_WAKE_EXEC, "--model"), "a --model on its ExecStart=")
MODEL_LOCAL = ROOT / MODEL.removeprefix(PI_DIR + "/")

_KIOSK_EXEC = one(unit(KIOSK_UNIT), "Service", "ExecStart") or ""
KIOSK_USER_DATA = _need(flag(_KIOSK_EXEC, "--user-data-dir"),
                        "a --user-data-dir on its ExecStart=").replace(
                            "%h", f"/home/{PI_USER}")
POLICY_ORIGIN = origin(url_in(_KIOSK_EXEC) or "")
POLICY_DIR = "/etc/chromium/policies/managed"

# The port the panel opens, and therefore the port the server has to be holding
# for the panel to be looking at it. Parsed out of the kiosk's URL for the
# reason the server's unit passes no `--port`: 8000 is already written in four
# places and a fifth copy here would be the one that drifts.
PI_PORT = urlsplit(POLICY_ORIGIN).port or 8000

# The line the monitor prints once the model is loaded and the stream is open,
# read out of wake.py so that rewording it cannot leave the last step of a
# verification waiting for a line nobody writes any more.
_BANNER = re.search(r'f"(listening on [^"{]*)',
                    (ROOT / "src/ai_terminal/wake.py").read_text())
BANNER = _BANNER.group(1) if _BANNER else "listening on "

# The server's nearest equivalent, read the same way for the same reason —
# but weaker than the monitor's, and the comment has to say so. main.py writes
# this at module scope, so it means the module imported and not that the socket
# is held; a server crash-looping on a port somebody else has writes it once
# per cycle. It is kept because it is the cheapest evidence that the import
# itself is not the thing failing. What proves the bind is `_holds_port`.
_STARTED = re.search(r'logger\.info\("(running revision) %s',
                     (ROOT / "src/ai_terminal/live/main.py").read_text())
STARTED = _STARTED.group(1) if _STARTED else "running revision"

# What systemd writes when it begins a stop, and the marker `_stopped_cleanly`
# splits a journal on. Not read out of anything, because it is systemd's word
# rather than this project's; the trailing space keeps it off `Stopped`, which
# is the line that closes the same stop.
STOPPING = "Stopping "

# The variable that line reports, and the one the deploy stamps into the unit.
# `main._revision()` reads it ahead of everything else because a copied tree
# has no `.git` to ask. Spelled rather than parsed, and pinned by driving
# `main._revision()` with it in `deploy_check` — a name matched out of the
# source would still agree with a main.py that had stopped reading it.
STAMP = "AI_TERMINAL_REVISION"
STATIC_STAMP = "AI_TERMINAL_STATIC_REVISION"

# And when that commit was made, which is what the panel's corner shows (#261).
# A second variable in the same drop-in rather than a file of its own: the two
# are one statement about one tree, and a deploy that wrote half of it would
# date a build by its SHA or name a build by its date.
BUILT_STAMP = "AI_TERMINAL_BUILT"


def revision() -> str:
    """The commit this Mac is about to copy, named as the server names it.

    The rule is `main._revision()`'s git branch, `-dirty` suffix and pathspec
    included, and `deploy_check` drives both against one fixture repo rather
    than trusting this comment. Two halves of it are load-bearing. The rsync
    copies the working tree, so an uncommitted edit is part of what lands and
    a bare SHA would read as though somebody had checked; and thirty other
    projects share this repo, so an unscoped `--dirty` would report edits that
    never reach the Pi.

    `"unknown"` for no git, no repo, and a git that said no — which is what an
    extracted tree is. Stamped anyway: a deploy that cannot name its own
    commit has to overwrite the last one's stamp, or the panel goes on
    reporting a SHA that is no longer what it is running.
    """
    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(("git", "-C", str(ROOT), *args),
                                  capture_output=True, text=True, timeout=5,
                                  env=_git_env())
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    head = git("rev-parse", "--short", "HEAD")
    if not head:
        return "unknown"
    return f"{head}-dirty" if git("status", "--porcelain", ".") else head


def built() -> str:
    """When the commit this Mac is about to copy was made. ISO 8601, or empty.

    `main._built()`'s rule, and `deploy_check` drives the two against one
    fixture repo rather than trusting this sentence. `%cI` is the commit's own
    date at its own offset, so the badge reads the hour `git log` does here —
    not the hour of the deploy, because redeploying an old commit is still
    that commit.

    Empty for no git, no repo, and a git that said no. Stamped anyway, for the
    reason `revision()` is: a deploy that cannot date its own commit has to
    overwrite the last one's stamp, or the panel goes on displaying a date
    belonging to a build it stopped running.
    """
    try:
        done = subprocess.run(
            ("git", "-C", str(ROOT), "show", "-s", "--format=%cI", "HEAD"),
            capture_output=True, text=True, timeout=5, env=_git_env())
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


# Resolved once, at import, so that building a plan runs nothing at all:
# `--dry-run` has to work on a plane with the Pi switched off, and
# `deploy_check`'s plan cases assert a rehearsal of all four phases spawns no
# subprocess whatsoever.
REVISION = revision()
BUILT = built()


def static_revision() -> str:
    """A digest of what /static serves, matching main._static_revision()."""
    static_dir = ROOT / "src" / "ai_terminal" / "live" / "static"
    digest = hashlib.sha256()
    for path in sorted(static_dir.rglob("*")):
        if not path.is_file():
            continue
        digest.update(path.relative_to(static_dir).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


STATIC_REVISION = static_revision()

# What the Pi's own `.env` has to set, and why nothing here can put it there:
# `.env` is on the exclude list because the Pi's differs from the Mac's.
# `SESSIONS_DB` and `GWS_CACHE_DIR` default against the process working
# directory, which is the unit's and so right by accident until anything runs
# the server from anywhere else; `BROWSE_CHROMIUM` has no default that exists,
# since Playwright ships no arm64 Linux browser.
#
# `BROWSE_CDP` and `BROWSE_ALLOWLIST` are here for a different reason: not
# that the server breaks without them, but that it quietly does something
# else. Unset, the kiosk's Chromium sits on `127.0.0.1:9222` with nothing
# connecting to it and every browse the household has ever seen came from a
# headless throwaway, while the README says otherwise — which went unnoticed
# from #144 to #208 because this row stayed green throughout. Demanded like
# any other name rather than as a pair, so the row is red on a Pi where the
# feature is off. It is a red only the owner can clear, `.env` being copied
# by hand, and that is the point of it.
#
# Names only — this probe never reads a value.
PI_ENV = ("BROWSE_CHROMIUM", "BROWSE_CDP", "BROWSE_ALLOWLIST",
          "SESSIONS_DB", "GWS_CACHE_DIR")

# And one of these, which is a different demand and needs its own tuple: the
# clock's weather is fetched by a background task with no browser to ask, and
# `screensaver.where` takes either — coordinates for nothing, or an address
# for one geocode per process. With neither, `where` returns None, the face
# carries no weather for the life of the machine and the journal says so
# every `REFRESH_S`. So the rule above is met by `HOME_COORDS` alone and by
# `HOME_LOCATION` alone, and putting both on `PI_ENV` would print a red row
# on a correctly configured Pi that nobody could ever clear.
PI_ENV_ANY = ("HOME_COORDS", "HOME_LOCATION")

APT_AUDIO = ("pipewire", "pipewire-alsa", "wireplumber", "i2c-tools")
# `fonts-noto-color-emoji` because the cards now carry emoji a model wrote
# (`SCREEN_STYLE` in `live/tools/_lang.py`). Without it Chromium draws tofu:
# `markEmoji` in `app.js` notices and sets `no-emoji`, but the only fallback
# behind that class is the calendar's Material Symbol, and there is no symbol
# to substitute for an arbitrary emoji in a sentence.
APT_DESKTOP = ("chromium", "labwc", "kanshi", "alsa-utils",
               "fonts-noto-color-emoji")
PACKAGES = APT_AUDIO + APT_DESKTOP

# Spelled here and in README.md's rsync block on purpose — see the module
# docstring. `.env` differs on the Pi, `gws_cache` belongs to whichever machine
# fetched the documents, and `screensaver` and `sessions.db*` live at the
# destination: losing them costs the household's conversations and buys a 2K
# generation to replace a picture that was already correct.
EXCLUDES = (".venv", ".env", "gws_cache", "__pycache__", "screensaver",
            "sessions.db*", "*.png")


# --------------------------------------------------------------------------
# the resource table
# --------------------------------------------------------------------------

# What a unit row means when the check reads it back. Two states and not one,
# because "installed" and "enabled" stop being the same question as soon as a
# topology ships a unit it does not want started — #10 turns the tunnel off on
# the shipped Pi while still requiring its file to be there.
RUNNING = "running"      # enabled, and active now
INSTALLED = "installed"  # on the Pi, and deliberately not started

OK, MISSING, WRONG = "ok", "MISSING", "WRONG"


@dataclass(frozen=True)
class Resource:
    """A file the Pi needs, where it goes, and what systemd does with it.

    `copy` is false for the tunnel, the one unit this script only inspects: it
    is installed by *Developing against the panel*, the shipped Pi wants it
    present and stopped, and writing it is not the installer's business.

    `compare` is the other half and a separate question, because conflating
    the two is #183: the kiosk was `copy=False` and so nothing wrote it, and
    the only reason anybody found out is that `compare` reads it back. A row
    can be both — a unit the deploy copies is still worth reading back, since
    `check` runs before `deploy` and the readback is what says the Pi is
    behind before anything is sent to it.
    """
    name: str
    source: Path
    dest: str
    unit_name: str = ""
    state: str = ""
    copy: bool = True
    compare: bool = False
    sudo: bool = False
    reload: str = ""
    target_name: str = ""

    @property
    def target_filename(self) -> str:
        return self.target_name or self.source.name

    @property
    def remote_path(self) -> str:
        return f"{self.dest}/{self.target_filename}"


UNIT_DIR = f"/home/{PI_USER}/.config/systemd/user"
KANSHI_DIR = f"/home/{PI_USER}/.config/kanshi"
LABWC_DIR = f"/home/{PI_USER}/.config/labwc"
UDEV_RULES_DIR = "/etc/udev/rules.d"
WIREPLUMBER_DIR = f"/home/{PI_USER}/.config/wireplumber/wireplumber.conf.d"
PI_CONFIG_DIR = f"/home/{PI_USER}/.config/ai-terminal"
GOOGLE_HOME_CREDENTIALS = ("home-mcp-token.json", "home-mcp-client.json")

# Where the deploy says which revision it just copied. systemd's own mechanism
# for a value that cannot be in the unit — the SHA is not knowable when the
# commit that carries the unit is written — and it sits beside the unit rather
# than inside `PI_DIR`, which is what keeps the next `rsync --delete` from
# taking it away with everything else the repo does not ship.
DROPIN = f"{UNIT_DIR}/{SERVER_UNIT.name}.d"
REVISION_CONF = f"{DROPIN}/revision.conf"

# The launch stamp recording what policy and kiosk unit Chromium read at
# launch (#212). Written when the kiosk is restarted, so that `kiosk_stale`
# can tell whether the running browser is on current files across any number
# of separate deploy and restart processes.
KIOSK_STAMP = f"{UNIT_DIR}/.kiosk-launch.stamp"

PANEL_RESOURCES = (
    Resource("the kanshi config", KANSHI_CONFIG, KANSHI_DIR,
             target_name="config"),
    Resource("the labwc rc.xml", LABWC_CONFIG, LABWC_DIR,
             target_name="rc.xml"),
    Resource("the touch calibration rule", TOUCH_RULES, UDEV_RULES_DIR,
             sudo=True,
             reload="sudo udevadm control --reload && sudo udevadm trigger"),
    Resource("the WirePlumber config", WIREPLUMBER_CONFIG, WIREPLUMBER_DIR),
)

RESOURCES = (
    Resource("the server", SERVER_UNIT, UNIT_DIR, "ai-terminal", RUNNING),
    Resource("the wake monitor's unit", WAKE_UNIT, UNIT_DIR,
             "ai-terminal-wake", RUNNING),
    Resource("the microphone grant", POLICY, POLICY_DIR, sudo=True),
    # Copied, since #183. It was not, on the argument that changing it blanks
    # the panel — but the panel is blanked for the length of one Chromium
    # start, and the alternative was a procedure that depends on somebody
    # remembering an `scp`. The day it failed: #145's `Wants=` removal was
    # carried to pi2 by hand at 09:29 and #144's `--remote-debugging-port`,
    # two and a half hours later, was not — so the port #162 attaches to was
    # closed on the one machine that runs the kiosk, and no deploy would have
    # opened it. The restart stays conditional on the file having changed, so
    # the blank costs a deploy that edited the unit and not every deploy.
    Resource("the kiosk", KIOSK_UNIT, UNIT_DIR, "ai-terminal-kiosk", RUNNING,
             compare=True),
    # The journal drop-in (#191). Copied to /etc/systemd/journald.conf.d so
    # journald sees it, with a reload so the running daemon re-reads
    # Storage=persistent and flushes before anything fails.
    Resource("the journal", JOURNAL, JOURNAL_DIR, sudo=True,
             reload="sudo systemctl restart systemd-journald && "
                    "sudo journalctl --flush"),
    *PANEL_RESOURCES,
    # Present and stopped. The file stays because it is how the panel is
    # developed against a Mac, and a shipped Pi that comes up with it enabled
    # forwards its own 8000 to a laptop — so the kiosk reaches whatever the
    # tunnel found rather than the server one directory away, and on a night
    # the Mac is asleep it reaches nothing at all. Not copied: a Pi that has
    # never had it is a Pi the shipped topology is happy with, and installing
    # it is *Developing against the panel*'s step.
    Resource("the tunnel", TUNNEL_UNIT, UNIT_DIR, "ai-terminal-tunnel",
             INSTALLED, copy=False, compare=True),
)


# --------------------------------------------------------------------------
# probes: one row of the table each, and a pure reader for its output
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Probe:
    """One question asked of the Pi, and how to read the answer.

    `read` takes the exit status and the combined output and returns a verdict
    and a detail to print. It is pure, so every row can be driven from a
    fixture in the suite with no host on the other end.
    """
    name: str
    command: str
    read: Callable[[int, str], tuple[str, str]]


def verdict(fine: bool, detail: str, otherwise: str = MISSING):
    return (OK, detail) if fine else (otherwise, detail)


def _last(out: str, cap: int = 70) -> str:
    lines = [line for line in out.strip().splitlines() if line.strip()]
    return lines[-1][:cap] if lines else "no output"


def _stamped(out: str) -> str:
    """What the revision drop-in read back as, for the failure message.

    Every `Environment=` line and not `_last`, which reports whichever came
    last: the drop-in carries three of them since #261, so a deploy that wrote
    a stale SHA under a fresh date would be reported by the date — the one
    half that was right.

    Only the half after the `===old===` marker, because the step `cat`s the
    previous drop-in before overwriting it and the previous one is not what
    failed. No marker means the step never got that far, so read it all.
    """
    _, marker, new = out.partition("===old===\n")
    lines = [line.strip() for line in (new if marker else out).splitlines()
             if line.strip().startswith("Environment=")]
    return "; ".join(lines)[:200] if lines else _last(out)


def _os(code: int, out: str):
    name = re.search(r'^PRETTY_NAME="([^"]*)"', out, re.M)
    kernel = re.search(r"^(\S+)\s+(aarch64|armv\w+)$", out, re.M)
    python = re.search(r"^Python (\d+)\.(\d+)", out, re.M)
    detail = ", ".join(part for part in (
        name.group(1) if name else "",
        " ".join(kernel.groups()) if kernel else "",
        python.group(0) if python else "") if part)
    new_enough = bool(python) and (int(python.group(1)),
                                   int(python.group(2))) >= (3, 11)
    return verdict(bool(name and kernel) and new_enough,
                   detail or _last(out), WRONG)


def _packages(names: tuple[str, ...]):
    def read(code: int, out: str):
        seen = dict(re.findall(r"^install ok installed (\S+) (\S+)$",
                               out, re.M))
        absent = [n for n in names if n not in seen]
        if absent:
            return MISSING, "missing " + ", ".join(absent)
        return OK, ", ".join(f"{n} {seen[n]}" for n in names)
    return read


def _uv(code: int, out: str):
    found = re.search(r"^uv (\S+)", out, re.M)
    return verdict(bool(found), found.group(0) if found else _last(out))


def _venv(code: int, out: str):
    ready = "wake-ok" in out
    return verdict(ready, "`wake --help` exits 0" if ready else _last(out))


def _alsa(code: int, out: str):
    pcms = [line for line in out.splitlines() if line.startswith("pipewire")]
    return verdict(bool(pcms), f"{len(pcms)} pipewire PCM"
                   + ("" if len(pcms) == 1 else "s"))


def _card(code: int, out: str):
    found = re.search(r"^card (\d+): .*?\b(seeed2micvoicec|USB Composite Device)\b", out, re.M)
    return verdict(bool(found),
                   f"card {found.group(1)}: {found.group(2)}" if found
                   else _last(out))


def _default_source(code: int, out: str):
    sources_match = re.search(r"Sources:\s*\n(.*?)(?:\n\s*[├└|─-]+\s*(?:Source endpoints|Streams|Video|Settings)|$)", out, re.DOTALL)
    section = sources_match.group(1) if sources_match else out
    found = re.search(r"^\s*[│|]?\s*\*\s*(\d+)\.\s*([^\n\[]+)", section, re.M)
    if not found:
        return MISSING, "no default source in wpctl status" if "Sources:" in out else _last(out)
    name = found.group(2).strip()
    is_usb = bool(re.search(r"\busb\b", name, re.I))
    return verdict(is_usb, name if is_usb else f"{name} (want USB Composite Device Mono)", WRONG)


def _overlay(code: int, out: str):
    found = re.search(r"^\d+:dtoverlay=(respeaker\S*)", out, re.M)
    return verdict(bool(found), found.group(1) if found else "not in config.txt")


def _present(what: str):
    def read(code: int, out: str):
        return verdict("present" in out, what if "present" in out else "absent")
    return read


def _dev_host(code: int, out: str):
    found = re.search(r"^AI_TERMINAL_DEV_HOST=(\S+)", out, re.M)
    return verdict(bool(found), found.group(1) if found else _last(out))


def _model(code: int, out: str):
    size = re.search(r"^(\d+)$", out.strip(), re.M)
    if not size:
        return MISSING, f"no file at {MODEL}"
    want = MODEL_LOCAL.stat().st_size if MODEL_LOCAL.is_file() else -1
    got = int(size.group(1))
    return verdict(got == want, f"{got} bytes"
                   + ("" if got == want else f", repo ships {want}"), WRONG)


def _env_keys(code: int, out: str):
    """Which of :data:`PI_ENV` the Pi's `.env` sets, by name and not by value.

    Two demands, not one. Every name in :data:`PI_ENV`, and at least one out
    of :data:`PI_ENV_ANY` — reported as a single row, because a person reading
    the check phase wants to know whether this file is finished, not which of
    two rules it broke. The pair is named with an "or" in the missing line so
    that the row says what would clear it.

    The command asks for the names alone, so there is nothing here to redact:
    this file holds the project, the key and the credentials, and a probe that
    printed one row of it into a terminal transcript would be a worse failure
    than the one it reports. A name reaches this function only when its value
    survives having quotes and spaces stripped, which is why "set" can be
    decided from presence — the emptiness is filtered out on the Pi, beside
    the value, and never travels.
    """
    found = {line.strip() for line in out.splitlines() if line.strip()}
    absent = [name for name in PI_ENV if name not in found]
    have_any = [name for name in PI_ENV_ANY if name in found]
    if not have_any:
        absent.append(" or ".join(PI_ENV_ANY))
    return verdict(not absent, "missing " + ", ".join(absent) if absent
                   else ", ".join((*PI_ENV, *have_any)) + " are set")


def _policy_file(code: int, out: str):
    found = re.search(r"^(\d+) (\S+) (\S+)$", out.strip(), re.M)
    if not found:
        return MISSING, _last(out)
    mode, owner, group = found.groups()
    return verdict((mode, owner, group) == ("644", "root", "root"),
                   f"{mode} {owner}:{group}", WRONG)


_PANEL_COOKIES_CMD = (
    f"python3 -c \"import sqlite3; "
    f"db = sqlite3.connect('file:{KIOSK_USER_DATA}/Default/Network/Cookies?mode=ro', uri=True); "
    f"cur = db.cursor(); "
    f"cur.execute(\\\"SELECT coalesce(sum(host_key like '%.google.com' AND name IN ('SID', '__Secure-1PSID')), 0), count(*) FROM cookies\\\"); "
    f"row = cur.fetchone(); "
    f"print(f'{{row[0]}} {{row[1]}}')\" 2>&1"
)


def _panel_session(code: int, out: str):
    """Check whether the panel's Chromium profile holds a Google session.

    `BROWSE_CDP` is demanded of every shipped Pi's `.env`, so a Pi whose
    `.env` row is green attaches to this profile. The check reads cookie counts
    only — never names, values, or accounts.
    """
    if "unable to open database file" in out or "no such table" in out:
        return MISSING, "no cookies database at " + KIOSK_USER_DATA
    parts = out.strip().split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        google_count, total_count = int(parts[0]), int(parts[1])
        if google_count > 0:
            return OK, (f"{google_count} Google session cookie"
                        f"{'s' if google_count != 1 else ''} "
                        f"({total_count} total)")
        return WRONG, f"0 Google session cookies ({total_count} total)"
    return WRONG, _last(out, cap=70)


def _linger(code: int, out: str):
    found = re.search(r"^Linger=(\w+)", out, re.M)
    return verdict(bool(found) and found.group(1) == "yes",
                   found.group(0) if found else _last(out), WRONG)


def _bias(code: int, out: str):
    # 0x19 is 0x86 during active capture and 0x06 at rest when DAPM powers down
    # the unused bias widget. Both are valid states depending on stream activity.
    found = re.search(r"0x([0-9a-f]{2})", out)
    ok = bool(found) and found.group(0) in ("0x86", "0x06")
    return verdict(ok,
                   f"reg 0x19 = {found.group(0)}" if ok
                   else (f"reg 0x19 = {found.group(0)} (want 0x06 or 0x86)"
                         if found else _last(out)),
                   WRONG)


def _unit_state(want: str):
    def read(code: int, out: str):
        if "could not be found" in out or "No such file" in out:
            return MISSING, "no unit file on the Pi"
        enabled = re.search(r"^(enabled|enabled-runtime|static|disabled)$",
                            out, re.M)
        active = re.search(r"^(active|inactive|failed|activating|unknown)$",
                           out, re.M)
        state = f"{enabled.group(1) if enabled else '?'}, " \
                f"{active.group(1) if active else '?'}"
        if want == RUNNING:
            fine = bool(enabled) and enabled.group(1) != "disabled" \
                and bool(active) and active.group(1) == "active"
        else:
            # Present and deliberately not started, which is two assertions and
            # not one. Reading `is-active` alone passes the exact state #10's
            # acceptance test turns on: somebody runs `systemctl --user stop
            # ai-terminal-tunnel` without the `disable`, every row here goes
            # green, and the reboot that follows brings the tunnel back onto
            # port 8000 ahead of the server. So `disabled` is required and not
            # merely hoped for — `enabled-runtime` is not it either, since it
            # survives everything except the boot nobody is watching.
            fine = bool(enabled) and enabled.group(1) == "disabled" \
                and not (active and active.group(1) in ("active",
                                                        "activating"))
        return verdict(fine, state, WRONG)
    return read


def _unit_probe(resource: Resource) -> Probe:
    stem = resource.unit_name
    return Probe(
        f"{resource.name} is {resource.state}",
        f"systemctl --user cat {stem} >/dev/null 2>&1 "
        f"&& echo file-present || echo 'Unit could not be found'; "
        f"systemctl --user is-enabled {stem} 2>&1; "
        f"systemctl --user is-active {stem} 2>&1",
        _unit_state(resource.state))


def _unit_matches(resource: Resource):
    """Whether the unit on the Pi is the unit this repo ships.

    `_unit_state` asks the manager what state the unit is in, which is a
    question about whichever unit the Pi already has. Nothing else asks
    whether that is the unit in `deploy/` — so an edit made here reads green
    on every Pi still running the old text. That is how #145's `Wants=`
    removal reached one Pi and no other: the file was fixed in this tree,
    `check` went green everywhere, and the tunnel kept winning the race for
    port 8000 on any Pi the installer had provisioned. #183 is the same
    sentence about #144's debugging port, which is why the kiosk is now
    copied as well as read back.

    Same reading as `policy_differs`: unreadable or absent counts as not this
    repo's, because a unit nobody can read is not one anybody has checked.
    """
    want = resource.source.read_text().strip()

    def read(code: int, out: str):
        if not out.strip():
            return MISSING, "no unit file on the Pi"
        return verdict(out.strip() == want,
                       f"matches deploy/{resource.source.name}"
                       if out.strip() == want
                       else f"differs from deploy/{resource.source.name}",
                       WRONG)
    return read


def _content_probe(resource: Resource) -> Probe:
    """Read a unit back off the Pi so the table can compare it, not trust it.

    For the rows that carry `compare`. A row the deploy does not copy is one
    somebody installed by hand once, and there is nothing between that hand
    and this repo but this probe. A row it does copy still wants it, because
    `check` runs before `deploy`: the readback is how the table says the Pi is
    behind while there is still time to do something about it, and it is the
    only row that would have caught #183.
    """
    return Probe(
        f"{resource.name}'s unit is the one in deploy/",
        f"cat {resource.dest}/{resource.unit_name}.service 2>/dev/null "
        "|| true",
        _unit_matches(resource))


def _sha256_matches(source: Path, remote_path: str):
    def read(code: int, out: str):
        found = re.search(r"\b([0-9a-f]{64})\b", out)
        if not found:
            return MISSING, f"no file at {remote_path}"
        got = found.group(1)
        want = hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else ""
        return verdict(got == want,
                       f"matches deploy/{source.name}"
                       if got == want
                       else f"differs from deploy/{source.name}",
                       WRONG)
    return read


def _file_probe(resource: Resource) -> Probe:
    return Probe(
        resource.name,
        f"sha256sum {resource.remote_path} 2>/dev/null || true",
        _sha256_matches(resource.source, resource.remote_path))


PROBES: tuple[Probe, ...] = (
    Probe("the OS, the kernel and the interpreter",
          "cat /etc/os-release; uname -mr; python3 -V",
          _os),
    Probe("the audio packages",
          "dpkg-query -W -f '${Status} ${Package} ${Version}\\n' "
          + " ".join(APT_AUDIO) + " 2>&1",
          _packages(APT_AUDIO)),
    Probe("the desktop packages",
          "dpkg-query -W -f '${Status} ${Package} ${Version}\\n' "
          + " ".join(APT_DESKTOP) + " 2>&1",
          _packages(APT_DESKTOP)),
    Probe("uv, at the absolute path the unit spells",
          f"{UV} --version 2>&1", _uv),
    Probe("the project's virtualenv",
          f"cd {PI_DIR} && .venv/bin/wake --help >/dev/null 2>&1 "
          "&& echo wake-ok || echo wake-broken",
          _venv),
    Probe("the ALSA route into the session's PipeWire graph",
          "aplay -L 2>/dev/null | grep '^pipewire' || true", _alsa),
    Probe("the capture card", "arecord -l 2>&1 || true", _card),
    Probe("the default audio source",
          "XDG_RUNTIME_DIR=/run/user/1000 wpctl status 2>&1 || true",
          _default_source),
    Probe("the HAT's overlay",
          "grep -n '^dtoverlay=respeaker' /boot/firmware/config.txt || true",
          _overlay),
    Probe("the journald drop-in, or there is no per-user journal",
          f"test -f {JOURNAL_DIR}/{JOURNAL.name} "
          "&& echo present || echo absent",
          _present("installed")),
    Probe("AI_TERMINAL_DEV_HOST, which the tunnel exits without",
          f"cat /home/{PI_USER}/.config/environment.d/ai-terminal.conf 2>&1",
          _dev_host),
    Probe("a current copy of this directory",
          f"test -d {PI_DIR}/deploy && echo present || echo absent",
          _present(f"{PI_DIR}/deploy is there")),
    Probe("the model the unit's --model names",
          f"stat -c %s {MODEL} 2>/dev/null || true", _model),
    # A name whose value is still there once quotes and spaces come off, and
    # the value is thrown away on the Pi before anything comes back.
    # `.env.example` ships `HOME_LOCATION=` with nothing on the end, so a file
    # made by copying it has the name and no address — `where` strips that to
    # "" and gives up, and a probe that matched the bare name would have
    # called the file finished. `GWS_CACHE_DIR=""` is the same nothing written
    # by somebody who has used a shell before, and python-dotenv hands the app
    # the same empty string for it, so the quotes have to come off here too.
    # `\047` is a literal single quote that fits inside the single-quoted awk
    # program; mawk on pi2 takes it. Still names only: awk runs on the far
    # side of the ssh and prints `$1`.
    #
    # The third `gsub` is the separators, and it is `BROWSE_ALLOWLIST`'s
    # (#229). That value is a comma-separated list, and `browser._allowlist`
    # drops every field that is blank — so `BROWSE_ALLOWLIST=" , , "` is the
    # empty list to the app and a set key to the two rules above, which is a
    # green row over a server whose attached browse refuses to start. Every
    # comma is removed rather than only the leading and trailing ones, since
    # this is only ever asked whether anything is left: a value with one real
    # character in it survives, whatever punctuation is around it. Applied to
    # every name and not just this one, because a bare comma is not a value
    # for any of them.
    Probe("the keys the Pi's own .env sets, which the rsync will not copy",
          r"""awk -F= '/^[A-Z_]+=/ {v = substr($0, index($0, "=") + 1); """
          r"""gsub(/^[ \t]+|[ \t]+$/, "", v); """
          r"""gsub(/^["\047]+|["\047]+$/, "", v); """
          r"""gsub(/[ \t,]/, "", v); """
          r"""if (v != "") print $1}' """
          f"{PI_DIR}/.env 2>/dev/null || true",
          _env_keys),
    Probe("the microphone grant, where Chromium reads managed policy",
          f"stat -c '%a %U %G' {POLICY_DIR}/{POLICY.name} 2>&1",
          _policy_file),
    Probe("the Google account in the panel's Chromium profile",
          _PANEL_COOKIES_CMD, _panel_session),
    Probe("linger, or the monitor is up only while somebody is watching",
          f"loginctl show-user {PI_USER} -p Linger 2>&1", _linger),
    Probe("the microphone bias DAPM keeps powering down",
          "sudo i2cget -f -y 1 0x18 0x19 2>&1", _bias),
) + tuple(_unit_probe(r) for r in RESOURCES if r.unit_name) \
  + tuple(_content_probe(r) for r in RESOURCES if r.unit_name and r.compare) \
  + tuple(_file_probe(r) for r in PANEL_RESOURCES)


# --------------------------------------------------------------------------
# plans: data, so that --dry-run is the plan printed rather than a second path
# --------------------------------------------------------------------------

SSH = ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10")

# Tokens that change the machine on the other end. Matched as words, so
# `is-enabled` is not `enable` and `i2cget` is not `i2cset`.
WRITE_TOKENS = ("apt-get", "install", "rsync", "scp", "tee", "mkdir", "rm",
                "cp", "mv", "chmod", "chown", "i2cset", "amixer",
                "enable-linger", "enable", "disable", "start", "stop",
                "restart", "daemon-reload", "sync", "flush")


@dataclass(frozen=True)
class Step:
    """One command, and what to make of the way it came back.

    `verify` is how a phase asserts rather than hopes: it takes the exit
    status and the output and returns a message when the step did not achieve
    what it was for. A step with no `verify` has succeeded when it exits zero.
    """
    name: str
    argv: tuple[str, ...]
    verify: Callable[[int, str], str | None] | None = None
    when: str = ""


def remote(host: str, command: str) -> tuple[str, ...]:
    return (*SSH, host, command)


# The word a command prints instead of its output when the Pi has not got
# the tool it needed. Chosen here rather than read off the shell, and not
# a word anything on the Pi says for its own reasons.
NO_TOOL = "ai-terminal-no-such-tool:"


def needs(tool: str, command: str) -> str:
    """`command`, or a line naming `tool` when the Pi has not got it.

    These commands end `2>&1 || true`, so that what a step means is decided
    by its reader and not by an exit code. That also folds the shell's own
    `command not found` into the blob the reader scans, and a reader looking
    for a line cannot then tell "it is not there" from "I could not look":
    `_holds_port` answers a missing `ss` with "nothing is listening on
    8000", which is a false alarm on the last step of the deploy.

    Reading the shell's message back is not the fix. bash writes
    `ss: command not found`, dash writes `ss: not found`, and nothing here
    chooses the shell that `ssh host 'cmd'` starts — so a guard written
    against either wording is one that quietly never fires under the other,
    which is this same bug one level up.
    """
    return (f"if command -v {tool} >/dev/null 2>&1; then {command}; "
            f"else echo '{NO_TOOL} {tool}'; fi")


def _absent(out: str) -> str | None:
    """The tool a `needs()` command reported missing, or None.

    Matched as the whole head of a line. `journalctl -n 60` quotes command
    lines, and a substring test would read the sentinel back out of
    something the Pi merely repeated.
    """
    for line in out.splitlines():
        head, _, tool = line.strip().partition(" ")
        if head == NO_TOOL and tool:
            return tool
    return None


def mutating(argv: tuple[str, ...] | list[str] | str) -> str | None:
    """The first token in a command that would change the Pi, or None.

    Read back off the command rather than declared beside it. A `writes=False`
    sitting next to a step is a claim somebody has to remember to update; this
    is the command itself, so a write added to the `check` plan fails the suite
    on the next run instead of hiding behind a stale flag.
    """
    if isinstance(argv, str):
        line = argv
    else:
        if argv and argv[0] == "rsync" and "--dry-run" in argv:
            return None  # rsync's own rehearsal reports and changes nothing
        line = " ".join(argv)
    # A script hides its own writes from a scan of the command that starts it:
    # `bash tools/mic_setup.sh` reads as innocent and runs `i2cset`. Anything
    # ending in .sh counts as a write for that reason alone.
    script = re.search(r"(\S+\.sh)\b", line)
    if script:
        return script.group(1)
    for token in WRITE_TOKENS:
        if re.search(rf"(?:^|[\s'\";|&(]){re.escape(token)}(?:[\s'\";|&)]|$)",
                     line):
            return token
    return None


def check_plan(host: str) -> tuple[Step, ...]:
    return tuple(Step(probe.name, remote(host, probe.command))
                 for probe in PROBES)


def _bias_stuck(code: int, out: str) -> str | None:
    """Whether `mic_setup.sh` read 0x19 back as the value it had just written.

    This used to be `"0x86" in out`, which cannot fail. The script prints
    `BIAS reg 0x19 = 0x06 (want 0x86)`, so the literal is in the label whatever
    the register holds — and the register not holding it is the one failure the
    step exists for: the `i2cset` at `mic_setup.sh:35` returns 0 and DAPM
    powers the widget straight back down.

    `_bias` is not reusable here. It takes the first `0x..` in the output and
    the HPF line above prints one, so on this script's output it reports 0x0C's
    value in 0x19's row.
    """
    found = re.search(r"^BIAS reg 0x19 = (\S+)", out, re.M)
    if not found:
        return f"mic_setup.sh printed no bias readback: {_last(out)}"
    got = found.group(1)
    if got.lower() != "0x86":
        return f"mic_setup.sh left the bias at {got}, want 0x86"
    return None


def install_plan(host: str) -> tuple[Step, ...]:
    """The prerequisites, every one of them re-runnable.

    Nothing here appends, and nothing here is conditional on what is already
    on the Pi: `apt-get install -y` on a package that is present is a no-op,
    `uv sync` on a current lock is a no-op, `enable-linger` on a lingered
    account is a no-op, and `mic_setup.sh` sets registers to constants. So the
    phase run twice produces the same table, which `deploy_check` pins.
    """
    return (
        Step("the packages the monitor and the panel need",
             remote(host, f"sudo apt-get install -y {' '.join(PACKAGES)}")),
        Step("the project's own dependencies",
             remote(host, f"cd {PI_DIR} && {UV} sync")),
        Step("linger, without which this is not a kiosk",
             remote(host, f"sudo loginctl enable-linger {PI_USER}")),
        # mic_setup.sh re-reads 0x19 and 0x0C from inside a capture on its way
        # out, which is the only reading of 0x0C that means anything.
        Step("the mixer state DAPM resets on every sync",
             remote(host, f"cd {PI_DIR} && bash tools/mic_setup.sh"),
             verify=_bias_stuck),
    )


def deleting(itemized: str) -> list[str]:
    """The paths an `rsync --delete --itemize-changes` rehearsal would remove."""
    return [m.group(1).strip()
            for m in re.finditer(r"^\*deleting\s+(.+)$", itemized, re.M)]


def overwriting(itemized: str) -> list[str]:
    """The paths the same rehearsal would send, replacing what is there.

    The other half of the same danger, and the half that does not announce
    itself. README.md:1456-1462 puts it plainly: a destination-owned file is
    *removed* by `--delete` on a machine that has never run the server, and
    *overwritten* by that machine's own copy on one that has. The Mac this
    deploys from has run the server, so it holds a `sessions.db` and a
    `screensaver/` of its own — an exclude dropped here never produces a
    `*deleting` line at all, and a guard that read only those would have been
    silent on the one machine it runs on.

    An itemised line is a run of flag characters, a space, then the path. The
    run is not a fixed width to match on: rsync 3 writes eleven of them and
    the rsync macOS ships writes nine, so this anchors on the first character
    instead. A leading `.` is "no change" and is not a transfer; `>`, `<`, `c`
    and `h` are.
    """
    return [m.group(1).strip()
            for m in re.finditer(r"^[<>ch]\S*\s+(.+)$", itemized, re.M)]


# What the destination owns and the repo does not. A stale source file from an
# older rsync is exactly what --delete is for and must not stop a deploy; one
# of these is an exclude that has gone missing, and deleting it costs data no
# copy of this repo can put back.
STATE_LIKE = ("sessions.db*", ".env", "gws_cache", "screensaver", ".venv",
              "background.json", "*.db", "*.db-wal", "*.db-shm")


def state_like(paths: list[str]) -> list[str]:
    """The entries in a would-delete list that are state rather than source."""
    found = []
    for path in paths:
        trimmed = path.rstrip("/")
        parts = trimmed.split("/")
        if any(fnmatch(candidate, pattern)
               for pattern in STATE_LIKE
               for candidate in (trimmed, parts[0], parts[-1])):
            found.append(path)
    return found


def _guard(code: int, out: str) -> str | None:
    removed = state_like(deleting(out))
    replaced = state_like(overwriting(out))
    if not removed and not replaced:
        return None
    said = []
    if removed:
        said.append("--delete would remove " + ", ".join(removed))
    if replaced:
        said.append("this Mac's own copy would overwrite "
                    + ", ".join(replaced))
    return ("; ".join(said) + ". That is state the repo cannot put back, so "
            "an exclude is missing: fix EXCLUDES and README.md together "
            "before deploying.")


def _rsync(host: str, dry: bool) -> tuple[str, ...]:
    excludes = tuple(arg for name in EXCLUDES
                     for arg in ("--exclude", name))
    rehearsal = ("--dry-run", "--itemize-changes") if dry else ()
    return ("rsync", "-a", "--delete", *rehearsal, *excludes,
            f"{ROOT}/", f"{host}:{PI_DIR}/")


def deploy_plan(host: str,
                home_config_dir: Path = Path.home() / ".config" / "ai-terminal") -> tuple[Step, ...]:
    """Every resource the table says to copy, and the tree the first of them is.

    The rsync is not a fourth thing bolted on: it is what puts the model under
    the unit's `WorkingDirectory`, and the unit is a five-second restart loop
    without it. So the copy is followed by a stat of the exact path parsed out
    of the unit, rather than by trusting that the transfer covered it.

    It ends by writing down which revision it copied, for the same reason: the
    copy cannot work that out afterwards. `restart` picks the drop-in up on
    its `daemon-reload`, and a `restart` run on its own deliberately leaves
    the stamp alone — it copied nothing, so it has nothing to say about what
    is there.
    """
    steps = [
        Step("what --delete would remove, before it removes it",
             _rsync(host, dry=True), verify=_guard),
        Step("the project tree", _rsync(host, dry=False)),
        Step("the model, at the path the unit's --model names",
             remote(host, needs("stat",
                                f"stat -c %s {MODEL} 2>/dev/null || true")),
             verify=lambda code, out:
                 None if _model(code, out)[0] == OK
                 else f"{MODEL}: {_model(code, out)[1]}"),
        Step("somewhere for the user units to live",
             remote(host, f"mkdir -p {UNIT_DIR} {KANSHI_DIR} {LABWC_DIR} {WIREPLUMBER_DIR}")),
    ]
    for row in RESOURCES:
        if not row.copy:
            continue
        if not row.sudo:
            dest_spec = row.remote_path if row.target_name else f"{row.dest}/"
            steps.append(Step(row.name,
                              ("scp", str(row.source), f"{host}:{dest_spec}")))
        else:
            # `scp` cannot create /etc/chromium/policies/managed and on this Pi it
            # does not exist, so the file goes to /tmp and `install -D` makes the
            # path on the way in.
            install_cmd = (
                f"sudo install -D -o root -g root -m 644 -t {row.dest} /tmp/{row.source.name}"
                if not row.target_name else
                f"sudo install -D -o root -g root -m 644 /tmp/{row.source.name} {row.remote_path}"
            )
            steps += [
                Step(f"{row.name}, by way of /tmp",
                     ("scp", str(row.source), f"{host}:/tmp/")),
                Step(f"{row.name}, where the system reads it",
                     remote(host, install_cmd)),
            ]
        if row.reload:
            steps.append(Step(f"re-reading {row.name}, so changes take effect",
                              remote(host, row.reload)))
    home_config = Path(home_config_dir)
    cred_steps: list[Step] = []
    for filename in GOOGLE_HOME_CREDENTIALS:
        local_file = home_config / filename
        if local_file.is_file():
            size = local_file.stat().st_size
            cred_steps.append(
                Step(f"the Google Home credentials: {filename} ({size} bytes)",
                     ("scp", str(local_file), f"{host}:{PI_CONFIG_DIR}/{filename}"))
            )
            cred_steps.append(
                Step(f"the Google Home credentials mode 600: {filename}",
                     remote(host, f"chmod 600 {PI_CONFIG_DIR}/{filename}"))
            )
    if cred_steps:
        steps.append(Step("somewhere for the Google Home credentials to live",
                          remote(host, f"mkdir -p {PI_CONFIG_DIR}")))
        steps.extend(cred_steps)
    # Last, because it is a statement about the tree the steps above copied.
    # Without it `/api/config` answers `"unknown"` after an installer run
    # while the README's manual block yields a SHA, and #10's verification
    # step 2 — the deployed SHA, read from a laptop across the house — has
    # nothing to read. Written every time, including when this Mac cannot
    # name its own commit: a stale stamp from the last deploy is the one
    # answer worse than `"unknown"`.
    stamp = shlex.quote(f"[Service]\\nEnvironment={STAMP}={REVISION}\\n"
                        f"Environment={STATIC_STAMP}={STATIC_REVISION}\\n"
                        f"Environment={BUILT_STAMP}={BUILT}\\n")
    steps.append(Step(
        "which revision that tree is and when it was made, since a copy has "
        "no .git to ask",
        remote(host, f"mkdir -p {DROPIN} && touch {REVISION_CONF} "
                     f"&& cat {REVISION_CONF} "
                     f"&& printf '===old===\\n' && printf {stamp} > "
                     f"{REVISION_CONF} && cat {REVISION_CONF}"),
        # An empty BUILT (deployed from an extracted tree without git) cannot
        # be verified against a build timestamp. It is still written so stale
        # stamps are cleared, but verification is omitted rather than reporting
        # an unverified timestamp as verified (#286).
        verify=_verify_stamp if BUILT else None))
    return tuple(steps)


def _verify_stamp(code: int, out: str) -> str | None:
    # Both lines, not one. The drop-in now carries three `Environment=` lines
    # and a `_last()` tail names whichever came last, so a run that wrote a
    # fresh date over a stale SHA would be reported by the date and read as a
    # success. #261.
    #
    # When `BUILT` is empty (e.g. deployed from a tree without git), an
    # unanchored `"AI_TERMINAL_BUILT="` substring test becomes a prefix match
    # that any existing timestamp satisfies, failing open (#286). Refuse to
    # verify without a timestamp, rather than letting an empty BUILT match.
    if not BUILT:
        return f"the drop-in cannot be verified without a build timestamp: {_stamped(out)!r}"
    if f"{STAMP}={REVISION}" not in out or f"{BUILT_STAMP}={BUILT}" not in out:
        return f"the drop-in reads back as {_stamped(out)!r}"
    if "===old===" in out:
        old, _, new = out.partition("===old===\n")
        old_m = re.search(rf"(?:Environment=)?{STATIC_STAMP}=([a-f0-9]+)", old)
        new_m = re.search(rf"(?:Environment=)?{STATIC_STAMP}=([a-f0-9]+)", new)
        old_val = old_m.group(1).strip() if old_m else ""
        new_val = new_m.group(1).strip() if new_m else ""
        if old_val and old_val == new_val:
            print("           the panel is already on these bytes")
        else:
            print("           the panel will reload")
    return None


def restart_plan(host: str, kiosk_stale: bool = True) -> tuple[Step, ...]:
    """Bring the server and the monitor up, then prove they came up and stayed.

    Several observations in the README's order, because any of them can hold
    while the rest do not. `is-active` on its own is the weakest: `Restart=
    always` means a unit crash-looping on a missing model reports `active` to
    anybody who looks at the right moment. For the server the strongest is
    neither systemd's nor the journal's — it is `ss`, which is the only one
    that can tell a process that took the socket from one that lost it.

    The server is `restart` and not `enable --now`, because the phase before
    this one has just replaced the tree underneath it: a unit already up goes
    on serving the copy it imported, and `--now` on an active unit is a no-op
    that reports success. `enable` is separate and stays, since that is the
    half that survives a reboot.

    The monitor takes those two halves too, and until #228 it took only the
    first. `--now` is defined as start "after the specified unit file
    operations succeed": it starts a stopped unit and does nothing whatever to
    a running one, so every deploy since the last reboot left the wake monitor
    on the code it imported at boot while `check`, which compares files, read
    green. Measured on pi2: `ActiveEnterTimestamp` for this unit was the boot
    minute `who -b` reports, with a `wake.py` on the disk beside it that the
    running process had never read.

    Unconditional, like the server's and unlike the kiosk's. The cost is the
    model load, and the figure that applies here is the warm one: four cold
    boots in this Pi's journal put `listening on` 10 or 11 s after `Started`,
    but the one mid-session restart in the same journal took 2 s, and a deploy
    is a mid-session restart. Those seconds are deaf on their own account
    rather than under cover of the server's: that unit is back on its socket
    1-2 s after the `restart` two steps up returns, so this gap sits beside
    that one and not inside it, and a wake word said into it is not heard at
    all. Conditional is the worse trade even so. The kiosk's reason does not
    transfer — its restart costs a blanked panel, which is the thing somebody
    standing in front of it can see — and a `wake_stale` mirror of
    `kiosk_stale` would inherit #212: sampled above the phase loop, `deploy`
    and `restart` as two processes restart the monitor zero times, which is
    the failure this step exists to remove.

    `--now` goes rather than staying beside the restart: `restart` starts a
    unit that is not running — `systemctl(1)`, restart: "If the units are not
    running yet, they will be started" — and does not ask whether it is
    enabled.

    The kiosk takes the same two halves and in the same order, now that #183
    has the deploy copying its unit: `enable`, which is unconditional and
    idempotent and is what makes `the kiosk is running` true across a reboot,
    and a `restart`, which is not. `kiosk_stale` is true when either the
    managed policy or the unit itself differed from this repo's *at the moment
    this process started* — the two things a running Chromium only reads at
    launch. `main` samples it once, above the phase loop, so it describes one
    run and not the Pi now: a separate process started after a `deploy` has
    already written both files reads neither as stale and restarts nothing.
    That is why `deploy restart` is one invocation and not two.
    """
    stem = "ai-terminal-wake"
    steps = [
        Step("re-read the unit files, including any the deploy just wrote",
             remote(host, "systemctl --user daemon-reload")),
        Step("the server, enabled so it survives a reboot",
             remote(host, "systemctl --user enable ai-terminal")),
        Step("and restarted onto the tree the deploy just copied",
             remote(host, "systemctl --user restart ai-terminal")),
        Step("the monitor, enabled so it survives a reboot",
             remote(host, f"systemctl --user enable {stem}")),
        Step("and restarted too, since enabling a running unit does nothing",
             remote(host, f"systemctl --user restart {stem}")),
        Step("the kiosk, enabled and started so a stopped unit recovers",
             remote(host, "systemctl --user enable --now ai-terminal-kiosk")),
    ]
    if kiosk_stale:
        # Only when something it reads at launch changed. A restart blanks the
        # panel for several seconds and under the default all-four run an
        # unconditional one would do that every time. The unit is in the
        # condition as well as the policy, because `daemon-reload` above makes
        # systemd read the new file and does not make Chromium re-exec: a
        # deploy that copied a new `--remote-debugging-port` and stopped there
        # is #183 again, one step further along.
        steps.append(Step(
            "the kiosk, which reads the policy and its own command line "
            "only at launch",
            remote(host, "systemctl --user restart ai-terminal-kiosk && "
                         f"(sha256sum {POLICY_DIR}/{POLICY.name} "
                         f"{UNIT_DIR}/{KIOSK_UNIT.name} > {KIOSK_STAMP})"),
            when="the policy file or the kiosk unit on the Pi differed "
                 "from this repo's when this run started, or differs from "
                 "the launch stamp"))
    steps += [
        Step("the monitor is active",
             remote(host, f"systemctl --user is-active {stem} 2>&1"),
             verify=lambda code, out:
                 None if out.strip() == "active" else
                 f"is-active says {out.strip()!r}"),
        Step("and still active ten seconds later, so it is not restarting",
             remote(host, f"sleep 10; systemctl --user is-active {stem} 2>&1"),
             verify=lambda code, out:
                 None if out.strip() == "active" else
                 f"ten seconds later is-active says {out.strip()!r}"),
        Step("the journal shows the model loading and no traceback",
             remote(host, needs("journalctl",
                                f"journalctl --user -u {stem} -n 60 "
                                "--no-pager 2>&1 || true")),
             verify=_journal_reads_right),
        # chrome://policy is out of reach from a script, so the assertion is
        # that the file landed where Chromium reads it and grants the origin
        # the kiosk actually opens. A grant for the wrong port is not a
        # smaller grant, it is none.
        Step("the grant is readable by Chromium and names the kiosk's origin",
             remote(host, f"stat -c '%a %U %G' {POLICY_DIR}/{POLICY.name} "
                          f"2>&1; cat {POLICY_DIR}/{POLICY.name} 2>&1"),
             verify=_grant_landed),
        # Last, because the server spends seconds importing before it writes
        # anything and the monitor's ten-second wait above is already spent by
        # the time this runs. Asked of the journal and not only of systemd for
        # the reason the monitor's own pair gives: `Restart=always` answers
        # `active` for a process that has never finished starting.
        #
        # And asked of `ss` as well, because neither of the other two can see
        # the bind. `running revision` is written at import — main.py writes it
        # at module scope, before `run()` and long before uvicorn takes the
        # socket — so it survives in the journal from a cycle that then died on
        # the port. `ss` is the only one of the three that answers the question
        # the panel actually asks.
        Step("the server is active, serving, and holding the port itself",
             remote(host, "systemctl --user is-active ai-terminal 2>&1; "
                          + needs("ss", "ss -ltnp 2>&1 || true") + "; "
                          + needs("journalctl",
                                  "journalctl --user -u ai-terminal -n 60 "
                                  "--no-pager 2>&1 || true")),
             verify=_server_reads_right),
    ]
    return tuple(steps)


def _stopped_cleanly(out: str) -> str | None:
    """Whether systemd booked the most recent stop in this window as a fault.

    systemd logs this line only when it scored the stop as one, so its presence
    under the stop this phase has just performed is the whole of the test — on
    success the `Main process exited` line above it drops to debug and neither
    is written at all.

    What put it there is `uv run`. It waits for the child, and a child that
    dies of SIGTERM — which is what `systemctl stop` sends and what uvicorn
    re-raises on its way out — becomes an exit status of 128 + 15 rather than a
    signal systemd would have counted as clean. Without `SuccessExitStatus=143`
    in the unit, every clean stop writes one of these, and a journal that cries
    failure on every shutdown is one both readers below, and the person #10
    step 3 sends here, learn to read straight past.

    The most recent stop and not any stop in the window, because the window is
    a line count and lines are not time. The monitor's entire journal is 42
    lines — measured on pi2 — so `-n 60` hands back everything it has ever
    logged, faults from days before this fix included, and no amount of fixing
    can make a stop that already happened clean. Every stop opens with a
    `Stopping …` line, so the text after the last one is the only stop this
    phase could be responsible for. A window too short to hold even that says
    nothing rather than guessing; keeping it long enough is the job of the case
    that pins `-n 60`.
    """
    if STOPPING not in out:
        return None
    found = re.search(r"Failed with result '([^']*)'",
                      out.rsplit(STOPPING, 1)[-1])
    if not found:
        return None
    return (f"systemd booked a stop in this window as {found.group(1)!r}: a "
            "clean stop through `uv run` exits 143, so the unit needs "
            "SuccessExitStatus=143 or every restart reads as a fault")


def _journal_reads_right(code: int, out: str) -> str | None:
    if "Traceback" in out:
        return "the journal carries a traceback"
    if BANNER not in out:
        return (f"the journal has no {BANNER.strip()!r} line, so the model "
                "never loaded or the capture stream never opened")
    return _stopped_cleanly(out)


def _holds_port(out: str) -> str | None:
    """Whether the local server is the thing listening, off `ss -ltnp`.

    The one observation in this step that a crash-looping server cannot fake.
    `is-active` is true from `exec` onwards under `Type=simple`; the journal's
    `running revision` is written at import; and uvicorn's own bind failure is
    a bare `logger.error(exc)` with no traceback attached
    (uvicorn/server.py, the `except OSError` around `loop.create_server`), so
    the traceback test does not fire either. All three pass against a process
    that has never held the socket.

    The failure worth naming separately is the tunnel: `ssh -N -L 8000:...`
    binds the same loopback address, so "something answers on 8000" is not the
    same claim as "the server does". `ss -p` names the process, and on the Pi
    both run as the same user, so the name comes back without root.
    """
    listening = [line for line in out.splitlines()
                 if re.match(rf"LISTEN\b.*:{PI_PORT}\s", line.strip())]
    if not listening:
        return (f"nothing is listening on {PI_PORT}, so the unit is active "
                "and has not taken the socket")
    stolen = [line for line in listening if '"ssh"' in line]
    if stolen:
        return (f"ssh is holding {PI_PORT}, so the panel is looking down the "
                "tunnel at another machine and not at this server")
    return None


def _server_reads_right(code: int, out: str) -> str | None:
    lines = out.strip().splitlines()
    if not lines or lines[0].strip() != "active":
        return f"is-active says {(lines[0].strip() if lines else '')!r}"
    if "Traceback" in out:
        return "the journal carries a traceback"
    if STARTED not in out:
        return (f"the journal has no {STARTED!r} line, so the process is up "
                "and has not finished starting")
    # The bind first, because a server that never took the socket is the
    # failure this step was added for and it outranks how its last stop was
    # scored. Both, though: the stop this phase itself performed is in the same
    # window, and reading past it is exactly what the fault line trains.
    return _holds_port(out) or _stopped_cleanly(out)


def _grant_landed(code: int, out: str) -> str | None:
    state, detail = _policy_file(code, out)
    if state != OK:
        return f"{POLICY_DIR}/{POLICY.name}: {detail}"
    if POLICY_ORIGIN and POLICY_ORIGIN not in out:
        return (f"the installed policy does not grant {POLICY_ORIGIN}, "
                "which is the origin the kiosk unit opens")
    return None


# The order a full run takes them in, and it is the dict's own: `main` iterates
# `list(PHASES)` and nothing sorts it.
#
# `deploy` before `install`, which reads backwards and is the point. `install`
# runs `uv sync` and `bash tools/mic_setup.sh` **inside `PI_DIR`**, so both of
# them are the copy that is already on the Pi — and the rsync that would make
# them the copy you just committed is in `deploy`. Written the other way round,
# a commit that adds a dependency syncs the previous lock, `mic_setup.sh` is the
# previous script, and `restart` then brings up the new tree on dependencies
# nobody installed. Silently: every install step is idempotent (see
# `install_plan`), so all of it prints done.
#
# On a Pi with no `~/ai-terminal` yet it is worse than silent. `cd
# {PI_DIR} && uv sync` exits non-zero because the directory is not there, the
# run stops on the failed phase, and `deploy` — the phase that would have
# created it — is never reached. That is the first-boot path #10's acceptance
# test walks. #174.
#
# The rsync asks nothing of the Pi in return: it makes `PI_DIR` itself, and the
# `i2c-tools` apt that `mic_setup.sh` needs is inside `install`, ahead of it.
PHASES: dict[str, Callable[..., tuple[Step, ...]]] = {
    "check": check_plan,
    "deploy": deploy_plan,
    "install": install_plan,
    "restart": restart_plan,
}


# --------------------------------------------------------------------------
# running it
# --------------------------------------------------------------------------

def resolve(host: str) -> str:
    """The first name that answers, said out loud.

    `pi2` resolves on the Pi's own LAN and not necessarily from the Mac, where
    `raspberrypi.local` does — README.md:1877-1879 says so and every block still says
    `pi2`. A raw `Could not resolve hostname` in the middle of phase three
    costs more than a line at the top.
    """
    bare = host.split("@")[-1]
    for candidate in (bare, f"{bare}.local"):
        try:
            socket.getaddrinfo(candidate, 22)
        except OSError:
            continue
        return host.replace(bare, candidate)
    raise SystemExit(f"neither {bare} nor {bare}.local resolves from here")


def spawn(step: Step, timeout: float = 900) -> tuple[int, str]:
    done = subprocess.run(step.argv, capture_output=True, text=True,
                          timeout=timeout)
    return done.returncode, (done.stdout + done.stderr).strip()


def show(steps: tuple[Step, ...], title: str) -> None:
    print(f"\n{title} — {len(steps)} steps, nothing run")
    for step in steps:
        mark = mutating(step.argv)
        note = f"  [writes: {mark}]" if mark else ""
        if step.when:
            note += f"  [only when {step.when}]"
        print(f"  {step.name}{note}\n      "
              + " ".join(shlex.quote(part) for part in step.argv))


def acquire_lock(host: str, sha: str = REVISION) -> tuple[int, str]:
    """Take a lock on the Pi during writing runs to protect concurrent probes."""
    now = int(time.time())
    return spawn(Step("deploy lock",
                      remote(host, f"echo '{now} {sha}' > {LOCK_FILE}")))


def release_lock(host: str) -> tuple[int, str]:
    """Remove the lock file on the Pi."""
    return spawn(Step("deploy unlock",
                      remote(host, f"rm -f {LOCK_FILE}")))


def run_check(host: str) -> int:
    """Every probe, one line each, and the count of what is not right."""
    code, out = spawn(Step("deploy lock",
                           remote(host, f"cat {LOCK_FILE} 2>/dev/null || true")),
                      timeout=60)
    if code == 0 and out.strip():
        parts = out.strip().split()
        ts = None
        try:
            ts = int(parts[0]) if parts else None
        except ValueError:
            ts = None
        if ts is not None:
            sha = parts[1] if len(parts) > 1 else "unknown"
            age = max(0, int(time.time()) - ts)
            if age <= LOCK_MAX_AGE_S:
                print(f"deploy started {age} seconds ago (SHA: {sha}); readings are not trustworthy")
                return len(PROBES)
            print(f"warning: stale deploy lock ignored (started {age} seconds ago, SHA: {sha})")

    print(f"\ncheck — {len(PROBES)} rows against {host}")
    unmet = 0
    for probe, step in zip(PROBES, check_plan(host)):
        code, out = spawn(step, timeout=60)
        state, detail = probe.read(code, out)
        unmet += state != OK
        print(f"   {state:<7} {probe.name}: {detail}")
    print(f"   {len(PROBES) - unmet} of {len(PROBES)} rows are as they "
          "should be")
    return unmet


def run_steps(steps: tuple[Step, ...], title: str) -> int:
    print(f"\n{title} — {len(steps)} steps")
    for step in steps:
        code, out = spawn(step)
        problem = None
        # Before both of the others, and before `verify` above all: a reader
        # handed a blob with nothing in it answers with what it did not find,
        # and what it did not find is not what happened.
        missing = _absent(out)
        if missing:
            problem = (f"{missing} is not installed on the Pi, so this step "
                       "could not make the observation it is here to make")
        elif code != 0:
            problem = f"exit {code}: {_last(out)}"
        elif step.verify is not None:
            problem = step.verify(code, out)
        if problem:
            print(f"   FAILED  {step.name}\n           {problem}")
            return 1
        print(f"   done    {step.name}")
    return 0


def _differs(host: str, remote_path: str, source: Path) -> bool:
    """Whether the file at `remote_path` is not `source`.

    Unreadable or absent counts as different, which errs towards one kiosk
    restart rather than towards a browser still holding the old policy — or,
    since #183, the old command line.
    """
    code, out = spawn(Step("", remote(host, f"cat {remote_path} "
                                            "2>/dev/null || true")), 60)
    return out.strip() != source.read_text().strip()


def policy_differs(host: str) -> bool:
    """Whether the Pi's copy of the grant is not this repo's."""
    return _differs(host, f"{POLICY_DIR}/{POLICY.name}", POLICY)


def kiosk_unit_differs(host: str) -> bool:
    """Whether the kiosk unit on the Pi is not this repo's.

    Read before the deploy writes it, for the reason `policy_differs` is:
    afterwards the answer is always no, and the one deploy that first carries
    a new command line would be the one that never restarted onto it.
    """
    return _differs(host, f"{UNIT_DIR}/{KIOSK_UNIT.name}", KIOSK_UNIT)


def kiosk_stamp_differs(host: str) -> bool:
    """Whether current files on the Pi differ from the kiosk's launch stamp.

    A stamp is written at restart time, recording sha256 digests of the policy
    and kiosk unit. If the stamp is missing or its digests do not match, the
    running kiosk is stale (#212).
    """
    code, _ = spawn(Step("kiosk stamp",
                         remote(host, f"sha256sum -c {KIOSK_STAMP} 2>/dev/null")), 60)
    return code != 0


def parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install and check the AI terminal on the Pi.")
    parser.add_argument("phase", nargs="*", choices=tuple(PHASES) + ("is-behind",),
                        help="any number of phases, always run in the order a "
                             "full run takes them in; the default is all four")
    parser.add_argument("--host", default="pi2",
                        help="the Pi, resolved to .local if the bare name "
                             "does not answer (default: pi2)")
    parser.add_argument("--dry-run", action="store_true", help="print plan")
    parser.add_argument("--is-behind", action="store_true",
                        help="check if Pi revision is behind latest deployed commit")
    parser.add_argument("--if-behind", action="store_true",
                        help="only execute deployment phases if Pi is behind")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    if "is-behind" in args.phase or args.is_behind:
        return _check_behind(args)
    if args.if_behind and _skip_if_current(args):
        return 0
    order = [name for name in PHASES if name in args.phase] or list(PHASES)
    if args.phase and order != args.phase:
        print(f"running {' '.join(order)} — phases run in the order a full "
              "run takes them in, not the order they were asked for")

    if args.dry_run:
        host = args.host
        print(f"host: {host}, not resolved — this is a dry run")
        for name in order:
            plan = restart_plan(host) if name == "restart" else PHASES[name](host)
            show(plan, name)
        return 0

    host = resolve(args.host)
    print(f"host: {host} answered")
    # Sampled here rather than in the restart branch below, because deploy
    # writes both of these files two phases earlier: read afterwards they are
    # always current, and the kiosk would never be restarted on the run that
    # first installs the grant or the run that first carries a new command
    # line. The stamp covers the separate deploy-then-restart sequence (#212).
    # Only when a restart is actually coming — a standalone `check`,
    # `install` or `deploy` has no use for the answer.
    kiosk_stale = "restart" in order and (policy_differs(host)
                                          or kiosk_unit_differs(host)
                                          or kiosk_stamp_differs(host))
    failed = 0
    writing = [name for name in order if name != "check"]
    if "check" in order:
        unmet = run_check(host)
        # Under the all-four run the unmet rows are the reason to carry on
        # — install and deploy are what settle most of them — so `check`
        # reports here and only decides the exit code when it was asked
        # for on its own.
        if order == ["check"]:
            return 1 if unmet else 0

    if writing:
        code, out = acquire_lock(host, REVISION)
        if code != 0:
            print(f"   FAILED  acquire deploy lock\n           exit {code}: {out}")
            return 1
        try:
            for name in writing:
                if name == "restart":
                    plan = restart_plan(host, kiosk_stale)
                else:
                    plan = PHASES[name](host)
                failed = run_steps(plan, name)
                if failed:
                    return failed
        finally:
            release_lock(host)

    print("\nNothing here can hear, so nothing here can tell you the wake "
          "word works.\nThe only verification is at the panel:\n"
          f"    ssh {host} 'journalctl --user -u ai-terminal-wake -f'\n"
          "  then stand in front of it and say the phrase.")
    return failed


def _check_behind(args: argparse.Namespace) -> int:
    if args.dry_run:
        print(f"host: {args.host}, not resolved — dry run is-behind")
        return 0
    host = resolve(args.host)
    running = get_pi_revision(host)
    latest = latest_deployed_commit()
    if running is None:
        print(f"could not determine running revision on {host}")
        return 1
    behind = is_pi_behind(running)
    if behind:
        print(f"Pi is behind (running {running}, latest deployed commit {latest})")
        return 1
    print(f"Pi is up to date (running {running}, latest deployed commit {latest})")
    return 0


def _skip_if_current(args: argparse.Namespace) -> bool:
    if args.dry_run:
        print(f"host: {args.host}, not resolved — dry run --if-behind")
        return False
    host = resolve(args.host)
    running = get_pi_revision(host)
    latest = latest_deployed_commit()
    if running is not None and not is_pi_behind(running):
        print(f"Pi is up to date (running {running}, latest deployed commit {latest}); skipping deploy")
        return True
    if running is not None:
        print(f"Pi is behind (running {running}, latest deployed commit {latest}); proceeding with deploy")
    return False


DEPLOYED_PATHS: tuple[str, ...] = ("src", "deploy", "pyproject.toml", "uv.lock")


def latest_deployed_commit(
    repo_path: Path | str | None = None,
    ref: str | None = None,
    short: bool = True,
) -> str:
    """The latest commit on `ref` (defaulting to origin/main or HEAD) touching deployed paths.

    Deployed paths are `src`, `deploy`, `pyproject.toml`, and `uv.lock`.
    Returns the commit SHA (short or full) or empty string if no git or no matching commit.
    """
    path = Path(repo_path) if repo_path is not None else ROOT
    fmt = "%h" if short else "%H"

    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(("git", "-C", str(path), *args),
                                  capture_output=True, text=True, timeout=5,
                                  env=_git_env())
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    target_ref = ref
    if not target_ref:
        if git("rev-parse", "--verify", "origin/main"):
            target_ref = "origin/main"
        elif git("rev-parse", "--verify", "main"):
            target_ref = "main"
        else:
            target_ref = "HEAD"

    if (path / "2026-0906-raspi-ai-terminal").is_dir():
        paths = [f"2026-0906-raspi-ai-terminal/{p}" for p in DEPLOYED_PATHS]
    else:
        paths = list(DEPLOYED_PATHS)

    out = git("log", "-n", "1", f"--format={fmt}", target_ref, "--", *paths)
    return out or ""


def is_pi_behind(
    pi_revision: str,
    repo_path: Path | str | None = None,
    ref: str | None = None,
) -> bool:
    """Whether the Pi's running revision is behind the latest deployed commit.

    Compares `pi_revision` against `latest_deployed_commit(repo_path, ref)`.
    Returns True if Pi revision is missing/unknown or does not match latest.
    Returns False when Pi revision matches latest.
    """
    latest = latest_deployed_commit(repo_path=repo_path, ref=ref, short=True)
    if not latest:
        return False
    if not pi_revision:
        return True
    cleaned_pi = pi_revision.strip().removesuffix("-dirty")
    cleaned_latest = latest.strip().removesuffix("-dirty")
    if cleaned_pi == "unknown":
        return True
    if cleaned_pi == cleaned_latest:
        return False
    if len(cleaned_pi) >= 7 and len(cleaned_latest) >= 7:
        if cleaned_pi.startswith(cleaned_latest) or cleaned_latest.startswith(cleaned_pi):
            return False
    return True


def get_pi_revision(host: str = "pi2") -> str | None:
    """Query AI_TERMINAL_REVISION from systemd Environment on the Pi."""
    try:
        target = resolve(host)
    except (SystemExit, OSError):
        target = host
    code, out = spawn(Step("query pi revision",
                           remote(target, "systemctl --user show ai-terminal -p Environment 2>&1")),
                      timeout=10)
    if code != 0:
        return None
    m = re.search(r"AI_TERMINAL_REVISION=([a-f0-9]+)", out)
    return m.group(1) if m else None


pi_revision = get_pi_revision


if __name__ == "__main__":
    sys.exit(main())

# SERVER_UNIT (deploy/ai-terminal.service) sets Environment=ENABLE_MANAGEMENT_ROUTES=1
# so management routes (/api/gws/logout, /api/forget/{user_id}, /api/wake) remain
# enabled on the Pi kiosk while being disabled by default in demo deployments.

