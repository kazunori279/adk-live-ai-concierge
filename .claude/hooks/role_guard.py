#!/usr/bin/env python3
"""PreToolUse guard: no GitHub write without a role, and no role writing outside it.

The issue-looping loop runs four sessions — monitor, reviewer, coder, tester —
and the owner starts ad-hoc `aide` sessions beside it. All of them authenticate
to GitHub as the same login. Which one acted is carried by a comment
marker and by the convention that certain labels belong to one role. Both lived in
SKILL.md, which means they were enforced by whether a session had read and
remembered a document.

On 2026-09-10 a fourth session, started with the words `read this project`, inferred
the reviewer role from that document, filed #155 with `stage:ready` (reviewer-only)
and no marker, and exited four minutes later. Nothing stopped it and nothing noticed
until the missing marker was queried. The same day the monitor applied `stage:ready`
to #10 — also reviewer-only — and the coder applied `on hold`, which is owner-only.
Three role violations in one day, by three different sessions, none of them caught
at the time.

This hook moves both rules out of the document and into the harness. A session with
no role cannot write to GitHub at all, which is the admission control the loop never
had; a session with a role cannot post unsigned or touch another role's labels.

**What this is not.** It is not a security boundary. A session that wanted a role
could export CC_ROLE itself, exactly as it could have signed someone else's marker.
It defends against a session that never considered the question — which is every
case observed so far.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import urllib.parse

# The marker is the role name. The monitor signed `<!-- owner -->` until 2026-09-10,
# which read as the owner having spoken when it was a session speaking, so comments
# older than that carry the old marker and a search of the monitor's history needs both.
#
# `tester` joined on 2026-09-11, on the owner's word, after a merged and swept
# change left the weather blank in a browser and nothing in the loop noticed:
# the 22 check scripts call functions, and none of them opens the page.
#
# `aide` joined on 2026-09-13, also on the owner's word, and for the opposite
# reason to the other four: it is not part of the loop. The owner files issues
# from ad-hoc sessions when the monitor is busy, and those sessions were writing
# unsigned — #477's body and a comment on #473 are both in the timeline with no
# marker. The guard blocks a roleless write outright, so the only ways to have an
# ad-hoc session at all were to leave the fail-open gap in place or to give the
# owner a role to assign. This is that role: it may file and comment, and it may
# do nothing that moves the queue.
ROLES = {
    "monitor": "<!-- monitor -->",
    "reviewer": "<!-- reviewer -->",
    "coder": "<!-- coder -->",
    "tester": "<!-- tester -->",
    "aide": "<!-- aide -->",
}

# The ownership table in issue-looping-shared/SKILL.md ("Who may set what").
# add/remove are the roles allowed to apply and to clear.
#
# `stage:doing` became `stage:pr` on 2026-09-11, when the owner moved
# implementation to the reviewer's subagents: the reviewer applies it once its
# implementer's pull request is open and green, and the coder clears it at the
# merge. The reviewer keeps `remove` as well, for the one case the shared skill
# calls out — an issue closed by a merge that still carries the label because the
# coder's next command was the comment instead.
LABEL_RULES = {
    "stage:ready": {"add": {"reviewer"}, "remove": {"reviewer", "coder", "monitor"}},
    "stage:pr": {"add": {"reviewer"}, "remove": {"coder", "reviewer", "monitor"}},
    "stage:triage": {"add": {"coder", "reviewer", "monitor", "tester", "aide"}, "remove": {"reviewer", "monitor", "aide"}},
    "stage:verified": {"add": {"reviewer"}, "remove": {"reviewer"}},
    "on hold": {"add": set(), "remove": set()},
    "hold:monitor": {"add": {"monitor"}, "remove": {"monitor"}},
    # `needs:live` says the issue's acceptance cannot be settled by any
    # `tools/*_check.py`, because it needs the Pi or a live model. Anyone who
    # triages may apply it; taking it off is how the merge gate below gets
    # bypassed, so removing it is the moderator's, and the owner's by hand.
    "needs:live": {
        "add": {"reviewer", "monitor", "tester", "aide"},
        "remove": {"monitor", "aide"},
    },
    # `stage:live` is where a `needs:live` issue lands at the merge, because its
    # pull request said `Refs #N` and did not close it. The coder applies it in
    # place of clearing `stage:pr`. (The only other stage label the coder applies
    # is swapping `stage:pr` for `stage:triage` when refusing a PR, #654).
    "stage:live": {
        "add": {"coder", "reviewer", "monitor"},
        "remove": {"reviewer", "monitor"},
    },
    "P0": {"add": {"reviewer", "monitor", "tester", "aide"}, "remove": {"reviewer", "monitor"}},
    "P1": {"add": {"reviewer", "monitor", "tester", "aide"}, "remove": {"reviewer", "monitor"}},
    "P2": {"add": {"reviewer", "monitor", "tester", "aide"}, "remove": {"reviewer", "monitor"}},
}

# The tester sets a priority when it files and never moves one afterwards, which is
# the asymmetry the table above encodes: `add` yes, `remove` no. It has to say how
# bad what it saw looked; it never sees the queue, so it has no business re-ranking
# anyone else's issue.
#
# "It may raise but not lower" was the first draft of this rule and it is not
# expressible here. Priority is exactly-one-of, so raising P2 to P1 is
# `--add-label P1 --remove-label P2` — the same `remove` as lowering, and the guard
# reads labels one at a time rather than as a pair. Granting the remove would grant
# both. So the rule is the narrower one the guard can actually hold, and a tester
# that thinks an open issue is under-ranked says so in a comment and leaves it to
# the reviewer or the monitor.
#
# The tester sets priority at filing and never moves one afterwards. It files at
# `stage:triage` (listed in LABEL_RULES with add permission, but no remove) — and
# it may not merge or close.
#
# `aide` is the same shape for filing, but retains remove on `stage:triage` for
# manual triage. The close and merge refusals below name it alongside the tester.

# Advancing to stage:pr must clear prior stage labels so issues do not carry
# multiple stage labels (#333). The triage -> pr shortcut leaves stage:triage
# attached if the command only removes stage:ready.
STAGE_PR_REQUIRED_CLEARS = {"stage:triage", "stage:ready", "stage:owner"}

ROLE_DIR = pathlib.Path.home() / ".claude" / "cc-role"
HEARTBEAT_FILE = pathlib.Path(os.environ.get("ROLE_GUARD_HEARTBEAT_FILE", "/tmp/role_guard.last"))
RUNS_DIR = pathlib.Path(os.environ.get("ROLE_GUARD_RUNS_DIR", "/tmp/role_guard_runs"))

# A write is anything that changes state. Reads — view, list, plain api, search —
# fall through untouched, because the loop reads constantly and a guard that slows
# that down would be turned off within the hour.
WRITE_PATTERNS = [
    r"\bgh\s+issue\s+(create|comment|edit|close|reopen|delete|transfer|pin|unpin|lock|unlock)\b",
    r"\bgh\s+pr\s+(create|comment|edit|close|merge|ready|reopen|review)\b",
    r"\bgh\s+label\s+(create|edit|delete|clone)\b",
    r"\bgh\s+api\b[^|;&]*?(-X|--method)\s+(POST|PATCH|PUT|DELETE)",
    # `gh api` with a field or input flag is a POST even with no -X, which is
    # how issue dependencies are recorded: `-F issue_id=<id>`.
    r"\bgh\s+api\b[^|;&]*?(-F|-f|--field|--raw-field|--input)[=\s]",
]
BODY_PATTERNS = [
    r"\bgh\s+issue\s+(create|comment)\b",
    r"\bgh\s+pr\s+(create|comment)\b",
    r"\bgh\s+issue\s+edit\b[^|;&]*--body",
    # `close --comment` and `reopen --comment` post a comment while looking like a
    # state change, so they need the marker too. `close` and `reopen` on their own
    # carry no text and are left alone — the reviewer reopens constantly and a
    # marker rule scoped to writes rather than to comments would kill that path.
    r"\bgh\s+issue\s+(close|reopen)\b[^|;&]*--comment",
    r"\bgh\s+pr\s+(close|reopen)\b[^|;&]*--comment",
]
LABEL_FLAG = re.compile(
    r"(--add-label|--remove-label|--label|(?<![\w-])-l)[=\s]+"
    r"""("([^"]*)"|'([^']*)'|([^\s"']+))"""
)
# `--body-file` only. This feeds `cli_comment_status`, which reads `gh issue` and
# `gh pr` subcommands; none of them takes `-F`. Widening it to `-F` would make
# `gh issue create … -F foo=bar` try to open a file called `foo=bar`, fail, and be
# refused as an unreadable body. The REST `-F body=@…` route is read by
# `rest_comment_body`, which tokenises properly and never comes through here.
BODY_FILE = re.compile(r"""--body-file[=\s]+("([^"]*)"|'([^']*)'|([^\s"']+))""")
PR_CREATE = re.compile(r"\bgh\s+pr\s+create\b")
DRAFT_FLAG = re.compile(r"(?:--draft\b|(?<![\w-])-d\b)")
PR_BODY_FILE = re.compile(
    r"""(?:--body-file|(?<![\w-])-F)[=\s]+("([^"]*)"|'([^']*)'|([^\s"']+))"""
)

# The REST route to the same labels, which LABEL_FLAG structurally cannot see.
#
# Measured on 2026-09-11: the monitor applied `on hold` — declared owner-only,
# `{"add": set()}` — with `gh api -X POST .../issues/N/labels -f 'labels[]=on hold'`,
# and the guard allowed it, because nothing on that command line is a `--label`
# flag. This is not an exotic path anyone had to go looking for: it is the
# documented way to *delete* a label whose name contains a colon, because
# `gh issue edit --remove-label stage:ready` 404s unless the colon is
# percent-encoded, so every session that has cleared a stage label the hard way
# has already typed it once.
#
# Residual gap, left open on purpose: a `DELETE` on `/issues/N/labels` with no
# label name clears *every* label, and nothing in the command says which those
# were. Refusing it outright would be the honest rule, but nobody has run it, and
# a guard that refuses a command it has never seen is a guess.
REST_LABEL_PATH = re.compile(r"/issues/\d+/labels/([^\s\"'&|;]+)")
REST_LABEL_FIELD = re.compile(
    r"""(?:-f|-F|--field|--raw-field)[=\s]+"""
    r"""(?:"labels(?:\[\])?=([^"]*)"|'labels(?:\[\])?=([^']*)'|labels(?:\[\])?=([^\s"']+))"""
)
REST_METHOD = re.compile(r"(?:-X|--method)[=\s]+(\w+)")

# The REST route to comments, which BODY_PATTERNS structurally cannot see.
#
# #289 closed the REST bypass for labels and merges, but missed comments:
# on 2026-09-11 the tester posted an unsigned comment via
# `gh api repos/.../issues/N/comments -f body=...` (#314). All sessions
# authenticate as the same GitHub login, so the marker is the only audit
# trail of who acted.
#
# Comment writes whose body the guard cannot read (--input -, -F body=@-, stdin
# pipes, heredocs) are refused. Until 2026-09-13 unreadable bodies passed through
# as an intentional fail-open gap (#314), but after the gap fired ~5x a day on the
# loop's highest-authority post-merge verification comments (#470), hiding the
# payload is treated as a refusal rather than a pass. The caller must write the
# body to /tmp/<role>-<N>-body.md and pass --body-file.
REST_COMMENT_PATH = re.compile(r"/issues/\d+/comments\b")
REST_BODY_FIELD = re.compile(
    r"""(?:-f|-F|--field|--raw-field)[=\s]+"""
    r"""(?:"body=([^"]*)"|'body=([^']*)'|body=(@"([^"]*)"|@'([^']*)'|@[^\s"';&|]+|"([^"]*)"|'([^']*)'|[^\s"';&|]+))"""
)
REST_INPUT_FLAG = re.compile(
    r"""--input[=\s]+(?:"([^"]*)"|'([^']*)'|([^\s"';&|]+))"""
)
CLI_BODY_FLAG = re.compile(
    r"""(?:--body|-b)[=\s]+(?:"([^"]*)"|'([^']*)'|([^\s"';&|]+))"""
)
CLI_COMMENT_FLAG = re.compile(
    r"""--comment[=\s]+(?:"([^"]*)"|'([^']*)'|([^\s"';&|]+))"""
)

# Only the coder merges. The reviewer writes the code now (2026-09-11), so this is
# the one command that still keeps the session that writes apart from the session
# that lands — and the coder reading the diff first is what that authority is for.
# Both spellings, because `gh pr merge` is a thin wrapper over the second.
MERGE = re.compile(r"\bgh\s+pr\s+merge\b|/pulls/\d+/merge\b")

# Closing is a judgement about the backlog, so the tester does not get it. The REST
# form is `PATCH /issues/N` with `state=closed`, which is why the field is matched
# as well as the subcommand.
CLOSES = re.compile(
    r"\bgh\s+issue\s+close\b"
    r"|\bstate=closed\b"                      # -f / --field / --raw-field state=closed
    r"|--state[=\s]+closed\b"
    r"|[\"']state[\"']\s*:\s*[\"']closed[\"']"
)

# The hold label, 2026-09-13, on the owner's decision (#490).
#
# Measured the night before. The monitor commented "Hold this patch — it rebuilds
# the thing #454 was opened to remove" on #476 at 21:41Z, naming the function not
# to merge. PR #484 was opened at 21:45Z containing that function, merged at
# 21:53Z, and deployed to the Pi at 21:54Z. Nobody did anything wrong that can be
# pointed at: the coder picks work off `stage:ready` and `gh pr list`, and a hold
# written in prose is on neither. **The loop's start signals are labels and its
# reasoning is comment bodies, so an objection expressed as reasoning is
# structurally invisible to the thing it objects to.** Whether the hold was right
# does not enter into it — a wrong one would have been merged through just as
# fast, which is the tell that this is a protocol defect rather than a judgement
# one.
#
# Deliberately a second label rather than a share of `on hold`. That one is the
# owner parking an issue; this one is the monitor contesting a patch. They lift
# on different evidence and by different hands, and collapsing them into one
# string would make "the owner said wait" and "a session disagreed" indistinguish-
# able in the list view — which is the failure `stage:ready` already has.
HOLD = "hold:monitor"

# The two labels behind the live-verification gate, and the one line that ends it.
#
# Measured on 2026-09-14 across 178 closed issues: 171 of them closed within 90
# seconds of their merge, because the commit said `Closes #N` and GitHub did the
# rest. Every rule this loop has written about verifying afterwards is a rule
# about work that happens after the issue has already left the queue, and it is
# skipped at the rate you would expect. `needs:live` is the marker that says the
# check can only be run against the Pi or a live model; a pull request for such
# an issue writes `Refs #N` instead, the merge leaves the issue open at
# `stage:live`, and the close is the last thing to happen rather than the first.
LIVE = "needs:live"
LIVE_STAGE = "stage:live"

# What closes a `needs:live` issue: a comment carrying a line that names where
# the behaviour was actually observed. `live-verified: raspberrypi.local 2026-09-14`.
LIVE_EVIDENCE = re.compile(r"(?im)^[ \t>*-]*live-verified:[ \t]*\S")

# Which issue or pull request a close or a merge is aimed at, so the hold can be
# looked up. Both `gh issue close` and `gh pr merge` take the number anywhere in
# their flags and both default to the current branch when it is absent, so the
# segment is scanned for the first bare integer and an absent one is not an error.
CLOSE_SEGMENT = re.compile(r"\bgh\s+issue\s+close\b([^|;&]*)")
MERGE_SEGMENT = re.compile(r"\bgh\s+pr\s+merge\b([^|;&]*)")
BARE_NUMBER = re.compile(r"(?<![\w.=/-])#?(\d+)(?![\w.-])")
REST_ISSUE_TARGET = re.compile(r"/issues/(\d+)(?:\b|$)")
REST_PR_TARGET = re.compile(r"/pulls/(\d+)/merge\b")
REPO_FLAG = re.compile(r"""--repo[=\s]+("([^"]*)"|'([^']*)'|([^\s"']+))""")

# The one non-`gh` rule here, and the only one that binds all three roles equally.
#
# `GET /api/config` on the Pi answers with `maps_api_key` in the clear, because the
# browser needs it for Maps JS. So a health check that looks entirely innocent prints
# a live key into a transcript, and on Antigravity into a conversation store on disk.
# The monitor did it on 2026-09-10 and the coder did it twice on 2026-09-11; both had
# read the paragraph in the shared skill that names this exact endpoint and gives the
# substitute. Two roles, one day apart, same command. That is a harness problem.
CONFIG_FETCH = re.compile(r"/api/config")
# Requires a fetcher, so `grep -rn "/api/config" src/` still works. Reading the source
# is not calling the endpoint, and a rule that caught the grep would be turned off.
# `http`/`https` are deliberately absent: they match inside the URL itself, so a grep
# for the full `http://…/api/config` would be refused for containing its own scheme.
FETCHERS = re.compile(r"\b(curl|wget|httpie|xh|httpx|urlopen|requests\.get)\b")
# A python filter selects what it prints, so it is the shape the skill recommends.
SAFE_FILTER = re.compile(r"\|\s*[^|]*\bpython3?\b[^|]*\s-c\b")
DISCARDS_BODY = re.compile(r"-o\s+/dev/null")
USES_JQ = re.compile(r"\|\s*[^|]*\bjq\b")

SUBSTITUTE = (
    "Read the endpoint only if you need a field it alone has, and filter with python "
    "on the host: `curl -s localhost:8000/api/config | python3 -c \"import json,sys; "
    'd=json.load(sys.stdin); print(sorted(d))"` prints the field *names*. If what you '
    "want is which build is running, do not ask this endpoint at all — "
    "`systemctl --user show ai-terminal -p Environment` gives the claimed sha and "
    "`sha256sum` on a deployed file against `git show <sha>:./<file> | shasum -a 256` "
    "proves the content."
)


# The word for "refuse this" differs by host: Claude Code wants `block`, the
# Antigravity CLI wants `deny`. Set from whichever payload shape arrived — see
# read_call. Defaults to Claude Code's, which is what an unrecognised payload
# almost certainly is.
DENY = "block"

# Keys unique to each host's payload, matched against the *raw* stdin text
# rather than a parsed object on purpose: the fail-open in __main__ has to know
# which host to answer even when the payload is the reason it is running. See
# #268 — reading the host out of the parse meant a truncated payload left DENY
# at Claude Code's default, the agy branch printed no `decision`, and agy reads
# that as a refusal.
#
# The quoted-key form matters. A bare `toolCall` also appears inside the command
# being run, every time anyone greps this file — and JSON escapes the quotes
# around a string *inside* a string, so `\"toolCall\":` in a command does not
# match while `"toolCall":` in the envelope does.
CC_PAYLOAD = re.compile(r'"(?:tool_input|session_id)"\s*:')
AGY_PAYLOAD = re.compile(r'"(?:conversationId|toolCall)"\s*:')


def sniff_deny(raw: str) -> str:
    """Which word this host uses for a refusal, without parsing.

    Claude Code wins a tie and wins an unrecognised payload, because its shape
    fails to *no opinion* — the command still goes through the normal permission
    prompt — while agy's `{"decision": "allow"}` skips the prompt outright. When
    the guard cannot tell who is asking, the answer that still asks the owner is
    the right one.
    """
    if CC_PAYLOAD.search(raw):
        return "block"
    return "deny" if AGY_PAYLOAD.search(raw) else "block"


def block(reason: str) -> None:
    print(json.dumps({"decision": DENY, "reason": reason}))
    sys.exit(0)


def allow() -> None:
    if DENY == "deny":
        print(json.dumps({"decision": "allow"}))
    else:
        print("{}")
    sys.exit(0)


def resolve_role(session_id: str) -> str:
    """Env var first, then a file keyed by session id.

    The file exists so a session already running when this landed can be given a
    role without losing its context — relaunching the coder mid-fix to satisfy a
    guard would cost more than the guard saves.
    """
    role = os.environ.get("CC_ROLE", "").strip().lower()
    if role:
        return role
    if session_id:
        f = ROLE_DIR / session_id
        try:
            return f.read_text().strip().lower()
        except OSError:
            pass
    return ""


def record_heartbeat(session_id: str, role: str, command: str) -> None:
    """Record proof that the guard executed, making silent non-execution observable (#643)."""
    import datetime
    try:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        snippet = (command or "").strip().split("\n")[0][:80]
        entry = f"{now}\tpid={os.getpid()}\tsession={session_id or '-'}\trole={role or '-'}\tcmd={snippet}\n"
        with HEARTBEAT_FILE.open("a", encoding="utf-8") as f:
            f.write(entry)
        if session_id and session_id != "-":
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            sess_file = RUNS_DIR / session_id
            sess_file.write_text(f"{now}\t{role}\t{snippet}\n", encoding="utf-8")
    except OSError:
        pass


def check_alive(session_id: str | None = None, max_age_seconds: int = 900) -> int:
    """Verify that role_guard has executed recently, making silent failure observable (#643, #656)."""
    import time
    now = time.time()

    if session_id:
        sess_file = RUNS_DIR / session_id
        if not sess_file.is_file():
            print(f"role_guard heartbeat not found for session {session_id} in {RUNS_DIR}.", file=sys.stderr)
            return 1
        try:
            mtime = sess_file.stat().st_mtime
            age = now - mtime
            if age > max_age_seconds:
                print(f"role_guard heartbeat for session {session_id} is stale ({age:.0f}s old > {max_age_seconds}s limit).", file=sys.stderr)
                return 1
            print(f"role_guard is active for session {session_id} (heartbeat {age:.1f}s ago).")
            return 0
        except OSError as exc:
            print(f"role_guard heartbeat check failed for session {session_id}: {exc}", file=sys.stderr)
            return 1

    # If session_id is omitted: check RUNS_DIR for any active session
    if not RUNS_DIR.is_dir():
        print(f"role_guard runs directory not found ({RUNS_DIR}). The guard has not run.", file=sys.stderr)
        return 1

    try:
        session_files = [p for p in RUNS_DIR.iterdir() if p.is_file() and not p.name.startswith(".")]
    except OSError as exc:
        print(f"role_guard failed to scan runs directory ({RUNS_DIR}): {exc}", file=sys.stderr)
        return 1

    if not session_files:
        print(f"role_guard runs directory is empty ({RUNS_DIR}). No active sessions recorded.", file=sys.stderr)
        return 1

    active_sessions: list[tuple[str, float]] = []
    newest_mtime = -1.0

    for p in session_files:
        try:
            mtime = p.stat().st_mtime
            if mtime > newest_mtime:
                newest_mtime = mtime
            age = now - mtime
            if age <= max_age_seconds:
                active_sessions.append((p.name, age))
        except OSError:
            continue

    if not active_sessions:
        newest_age = now - newest_mtime if newest_mtime > 0 else 0
        print(
            f"role_guard heartbeat is stale across all {len(session_files)} sessions "
            f"(most recent was {newest_age:.0f}s old > {max_age_seconds}s limit).",
            file=sys.stderr,
        )
        return 1

    newest_session, newest_age = min(active_sessions, key=lambda item: item[1])
    print(
        f"role_guard is active ({len(active_sessions)} active session(s); "
        f"most recent {newest_session} heartbeat {newest_age:.1f}s ago)."
    )
    return 0


def labels_in(command: str) -> tuple[set[str], set[str]]:
    added: set[str] = set()
    removed: set[str] = set()
    for m in LABEL_FLAG.finditer(command):
        flag = m.group(1)
        value = next(g for g in (m.group(3), m.group(4), m.group(5)) if g is not None)
        target = removed if flag == "--remove-label" else added
        target.update(part.strip() for part in value.split(",") if part.strip())

    rest_added, rest_removed = rest_labels(command)
    return added | rest_added, removed | rest_removed


def rest_labels(command: str) -> tuple[set[str], set[str]]:
    """Labels moved through `gh api` rather than through `gh issue edit`.

    The method decides the direction: `DELETE` names the label in the path and
    removes it, anything else carries it in a `labels[]` field and adds it. `gh
    api` defaults to POST once a field flag is present, which is why the fallback
    is POST rather than GET.
    """
    added: set[str] = set()
    removed: set[str] = set()
    if "/labels" not in command:
        return added, removed

    m = REST_METHOD.search(command)
    method = (m.group(1) if m else "POST").upper()

    if method == "DELETE":
        for mm in REST_LABEL_PATH.finditer(command):
            removed.add(urllib.parse.unquote(mm.group(1)).strip())
        return added, removed

    for mm in REST_LABEL_FIELD.finditer(command):
        value = next(g for g in mm.groups() if g is not None)
        added.update(part.strip() for part in value.split(",") if part.strip())
    return added, removed


def rest_comment_body(command: str) -> tuple[bool, str | None]:
    """Inspect a REST comment command for its body.

    Returns (is_comment_post, body).
    - If is_comment_post is False: not a POST to /issues/<N>/comments.
    - If is_comment_post is True and body is None: unreadable body (stdin, `--input -`,
      `-F body=@-`, unreadable file, or omitted body) -> refused by caller.
    - If is_comment_post is True and body is a string: readable body to check.
    """
    if "api" not in command or not REST_COMMENT_PATH.search(command):
        return False, None

    m = REST_METHOD.search(command)
    method = (m.group(1) if m else "POST").upper()
    if method != "POST":
        return False, None

    raw_body: str | None = None
    input_file: str | None = None
    try:
        tokens = shlex.split(command)
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in ("-f", "-F", "--field", "--raw-field") and i + 1 < len(tokens):
                i += 1
                arg = tokens[i]
                if arg.startswith("body="):
                    raw_body = arg[5:]
            elif tok.startswith(("--field=", "--raw-field=", "-f=", "-F=")):
                arg = tok.split("=", 1)[1]
                if arg.startswith("body="):
                    raw_body = arg[5:]
            elif tok == "--input" and i + 1 < len(tokens):
                i += 1
                input_file = tokens[i]
            elif tok.startswith("--input="):
                input_file = tok.split("=", 1)[1]
            i += 1
    except ValueError:
        pass

    if raw_body is None and input_file is None:
        mf = REST_BODY_FIELD.search(command)
        if mf:
            raw_body = next(g for g in mf.groups() if g is not None)
            if raw_body.startswith('@"') and raw_body.endswith('"'):
                raw_body = "@" + raw_body[2:-1]
            elif raw_body.startswith("@'") and raw_body.endswith("'"):
                raw_body = "@" + raw_body[2:-1]
            elif raw_body.startswith('"') and raw_body.endswith('"'):
                raw_body = raw_body[1:-1]
            elif raw_body.startswith("'") and raw_body.endswith("'"):
                raw_body = raw_body[1:-1]
        mi = REST_INPUT_FLAG.search(command)
        if mi:
            input_file = next(g for g in mi.groups() if g is not None)

    if raw_body is not None:
        if raw_body.startswith("@"):
            file_path = raw_body[1:]
            if file_path == "-":
                return True, None
            try:
                return True, pathlib.Path(file_path).read_text(errors="replace")
            except OSError:
                return True, None
        return True, raw_body

    if input_file is not None:
        if input_file == "-":
            return True, None
        try:
            content = pathlib.Path(input_file).read_text(errors="replace")
        except OSError:
            return True, None
        try:
            data = json.loads(content)
            if isinstance(data, dict) and "body" in data:
                return True, str(data["body"])
        except Exception:
            pass
        return True, content

    return True, None


def cli_comment_status(command: str, marker: str) -> tuple[bool, bool]:
    """Check a CLI command matching BODY_PATTERNS for readable body and signature.

    Returns (has_readable_body, is_signed).
    - has_readable_body: False if stdin, pipe, heredoc, missing body, or unreadable file.
    - is_signed: True if the readable body contains the role marker.

    There is deliberately no test for `<<` here, and that is the whole subtlety.
    A heredoc feeding `gh` its body — `gh issue comment 1 <<EOF` — carries no body
    flag at all, so it falls through to the last line and is refused without one.
    A `<<` test would also refuse the *dominant* shape in this repo, which writes
    the heredoc to a file and then names that file:

        cat > /tmp/mon-470-body.md <<'MD' … MD
        gh issue comment 470 --body-file /tmp/mon-470-body.md

    That is one Bash call, it contains `<<`, and its body is on disk and perfectly
    readable. Refusing it would break every session in the loop for a case the
    fallthrough already handles.
    """
    m_file = PR_BODY_FILE.search(command) if PR_CREATE.search(command) else BODY_FILE.search(command)
    if m_file:
        path = next(g for g in (m_file.group(2), m_file.group(3), m_file.group(4)) if g is not None)
        if path == "-":
            return False, False
        try:
            head = pathlib.Path(path).read_text(errors="replace")[:400]
            return True, marker in head
        except OSError:
            return False, False

    m_body = CLI_BODY_FLAG.search(command)
    if m_body:
        return True, marker in command

    m_comment = CLI_COMMENT_FLAG.search(command)
    if m_comment:
        return True, marker in command

    return False, False


def read_call(payload: dict) -> tuple[str, str]:
    """The shell command and the session id, from either host's PreToolUse payload.

    #243 moved the coder onto the Antigravity CLI and left the reviewer and the
    monitor on Claude Code, so one guard now has to read two shapes:

        Claude Code   {"tool_input": {"command": ...}, "session_id": ...}
        Antigravity   {"toolCall": {"name": "run_command",
                                    "args": {"CommandLine": ...}},
                       "conversationId": ...}

    Nothing else about the guard differs — the same commands are refused for the
    same reasons — so the two are normalised here and the rest of the file never
    learns which CLI it is running under.

    The `args` key is read case-insensitively and under several spellings on
    purpose. Antigravity's own docs show `CommandLine`, but everything else in its
    hook payload is camelCase protojson, and a key this guard fails to find reads
    as "no command", which is a silent allow. Failing open on a *bug* is the right
    call (see __main__); failing open on a spelling is not.

    **It does not decide which host is asking, and used to.** It returned a
    refusal word beside the command, and `main` assigned that over the one
    `sniff_deny` had already read off the raw bytes — which undid #268 on the one
    path where the host is known and only the envelope is not (#277). The two
    unknowns are different: an envelope with no branch here says nothing about who
    sent it. `sniff_deny` is the only thing that answers that question now.
    """
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict) and tool_input.get("command"):
        return str(tool_input["command"]), str(payload.get("session_id", ""))

    call = payload.get("toolCall")
    if isinstance(call, dict):
        args = call.get("args")
        if isinstance(args, dict):
            wanted = {"commandline", "command", "cmd"}
            for key, value in args.items():
                if key.lower() in wanted and isinstance(value, str) and value:
                    return value, str(payload.get("conversationId", ""))
        return "", str(payload.get("conversationId", ""))

    return "", str(payload.get("session_id", ""))


def secret_check(command: str) -> None:
    """Refuse a fetch of /api/config that would print the key. Binds every role."""
    if not (CONFIG_FETCH.search(command) and FETCHERS.search(command)):
        return
    if DISCARDS_BODY.search(command):
        return  # a status-code probe never renders the body
    if USES_JQ.search(command):
        block(
            "This fetches `/api/config`, which answers with `maps_api_key` in the "
            "clear, and redacts it with `jq` — **which is not installed on pi2.**\n\n"
            "`jq: command not found` fails the pipe *after* `curl` has already printed "
            "the whole object, key included. A redaction that fails open is worse than "
            "no redaction, because you stop watching for the thing it was meant to "
            f"catch.\n\n{SUBSTITUTE}"
        )
    if SAFE_FILTER.search(command):
        return
    block(
        "This fetches `/api/config`, which answers with `maps_api_key` in the clear — "
        "it has to, because the browser needs it for Maps JS. Unfiltered, the key "
        "lands in your transcript, and on Antigravity in a conversation store on "
        "disk.\n\nThis has happened twice: the monitor on 2026-09-10 and the coder on "
        "2026-09-11, both having read the paragraph in the shared skill that names "
        f"this endpoint. Hence a refusal rather than another warning.\n\n{SUBSTITUTE}"
    )


def refuse_unreadable_body(role: str) -> None:
    block(
        f"Role is `{role}`, and comment writes whose body cannot be read "
        "(--input -, -F body=@-, stdin pipes or heredocs) are refused.\n\n"
        "Every session authenticates as the same GitHub login, so the "
        "marker is the only thing in the timeline that says which of you acted. "
        "Write the body to /tmp/<role>-<N>-body.md and pass --body-file."
    )


def gh_json(args: list[str], repo: str) -> object | None:
    """Run a read-only `gh` query and parse it, or return None meaning *unknown*.

    None is fail-open and every failure path returns it: `gh` missing, not signed
    in, offline, a number that does not exist, a timeout, malformed output. The
    guard's whole design is that it never bricks the loop, and a hold check that
    refused whenever GitHub was slow would be turned off within the day.

    This is the only place the guard touches the network, and it runs only on a
    close or a merge that every other rule has already allowed — so the cost is
    one query on the rarest commands in the loop, not on every Bash call.
    """
    if repo:
        args = args + ["--repo", repo]
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=8)
    except Exception:  # noqa: BLE001
        return None
    if done.returncode != 0:
        return None
    try:
        return json.loads(done.stdout)
    except ValueError:
        return None


def issue_labels(number: str, repo: str) -> set[str] | None:
    """Label names on one issue, or None when GitHub could not be asked."""
    data = gh_json(["gh", "issue", "view", number, "--json", "labels"], repo)
    if not isinstance(data, dict):
        return None
    return {
        str(lab.get("name"))
        for lab in (data.get("labels") or [])
        if isinstance(lab, dict) and lab.get("name")
    }


def held_issues(command: str) -> list[str]:
    """Issue numbers this command would close or merge that carry the hold.

    A merge is resolved through the pull request's linked issues
    (`closingIssuesReferences`, which is what GitHub populates from `Closes #N`),
    because the hold lives on the issue and the argument is a PR.
    """
    repo_match = REPO_FLAG.search(command)
    repo = ""
    if repo_match:
        repo = next(g for g in repo_match.groups()[1:] if g is not None)

    issues: list[str] = []
    prs: list[str] = []

    if CLOSES.search(command):
        segment = CLOSE_SEGMENT.search(command)
        if segment:
            found = BARE_NUMBER.search(segment.group(1))
            if found:
                issues.append(found.group(1))
        issues += REST_ISSUE_TARGET.findall(command)

    if MERGE.search(command):
        segment = MERGE_SEGMENT.search(command)
        if segment:
            found = BARE_NUMBER.search(segment.group(1))
            prs.append(found.group(1) if found else "")
        prs += REST_PR_TARGET.findall(command)

    for pr in prs[:3]:
        view = ["gh", "pr", "view"]
        if pr:
            view.append(pr)
        data = gh_json(view + ["--json", "closingIssuesReferences"], repo)
        if not isinstance(data, dict):
            continue
        for ref in (data.get("closingIssuesReferences") or [])[:5]:
            if isinstance(ref, dict) and ref.get("number"):
                issues.append(str(ref["number"]))

    held: list[str] = []
    for number in dict.fromkeys(issues):
        names = issue_labels(number, repo)
        if names and HOLD in names:
            held.append(number)
    return held


def refuse_held(role: str, numbers: list[str]) -> None:
    which = ", ".join(f"#{n}" for n in numbers)
    block(
        f"{which} carries `{HOLD}`, so it does not close and its pull request does "
        f"not merge — including for the {role}.\n\n"
        "The monitor puts that label on when it is contesting the patch itself "
        "rather than the diagnosis, and the thread says what it is contesting and "
        "what would lift it. Read the most recent `<!-- monitor -->` comment "
        f"on {which} before doing anything else.\n\n"
        "The hold is not a veto and it is not the owner's `on hold`. If you think "
        "it is wrong, say so on the thread — the monitor clears its own label, and "
        "a hold nobody argues with for a tick is a hold the monitor should be "
        "lifting. What is not available is landing the change while the objection "
        "is unanswered, which is what happened to #476 on 2026-09-12 and is why "
        "this label exists."
    )


def check_issue_close(command: str) -> None:
    """Refuse closing a `needs:live` issue until the thread says where it was seen.

    The other end of gate 4. Keeping the issue open past the merge only buys
    anything if closing it later costs something, and the something is one line
    naming the host or the model — `live-verified: raspberrypi.local 2026-09-14`. It is
    a line somebody types, so it is a weak gate; what it is not is a close that
    happens as a side effect of a merge nobody read.
    """
    repo_match = REPO_FLAG.search(command)
    repo = ""
    if repo_match:
        repo = next(g for g in repo_match.groups()[1:] if g is not None)

    numbers: list[str] = []
    segment = CLOSE_SEGMENT.search(command)
    if segment:
        found = BARE_NUMBER.search(segment.group(1))
        if found:
            numbers.append(found.group(1))
    numbers += REST_ISSUE_TARGET.findall(command)

    for number in list(dict.fromkeys(numbers))[:3]:
        names = issue_labels(number, repo)
        if names is None or LIVE not in names:
            continue
        data = gh_json(["gh", "issue", "view", number, "--json", "comments"], repo)
        if not isinstance(data, dict):
            continue  # fail open: an unreachable thread is not evidence either way
        bodies = [
            str(c.get("body") or "")
            for c in (data.get("comments") or [])
            if isinstance(c, dict)
        ]
        if any(LIVE_EVIDENCE.search(b) for b in bodies):
            continue
        block(
            f"#{number} carries `{LIVE}` and no comment on it says the behaviour was "
            "observed live, so it does not close yet.\n\n"
            "Run it where it can be seen — `raspberrypi.local` for anything on the device, a "
            "real session for anything against a model — and post what you saw, with "
            "a line reading\n\n"
            "```\nlive-verified: raspberrypi.local 2026-09-14\n```\n\n"
            "as its own line. The tester is the role with the browser and the Pi; "
            f"issues sitting at `{LIVE_STAGE}` are its queue.\n\n"
            "If the behaviour is not reachable from the rig either, say that on the "
            "thread and take the label off rather than closing around it — an issue "
            "closed with nobody having looked is the state this label exists to "
            "prevent."
        )


def refuse_unsigned_comment(role: str, marker: str) -> None:
    block(
        f"Role is `{role}`, so this comment must be signed `{marker}` on its first "
        "line.\n\nEvery session authenticates as the same GitHub login, so the "
        "marker is the only thing in the timeline that says which of you acted. Add "
        "it to the top of the body (or of the --body-file) and run this again."
    )


import ast

CONFLICT_MARKER = re.compile(r"^(<{7}|={7}|>{7})", re.MULTILINE)
SWEEP_SHA_PATTERN = re.compile(r"(?i)\bSweep\s+SHA\b[*`:\s]*([0-9a-fA-F]{7,40})")
SWEEP_LINE_PATTERN = re.compile(
    r"(?:^|[\r\n])[`*#\s-]*(?:(\d+)\s*(?:->|→)\s*)?(\d+)\s+green:\s*([^\r\n`]+)"
)
SWEEP_COUNT_PATTERN = re.compile(
    r"(?i)\b(\d+)\s*/\s*(\d+)\s*(?:scripts|check scripts|suites|green)\b"
)
SWEEP_WAIVER = re.compile(
    r"(?i)\bSweep\b[*`:\s]*waived\s*[-–—:]\s*"
    r"(loop/process|docs\s+only|owner\s+ruling)\b"
)


def load_sweep_record(sha: str) -> dict | None:
    """The provenance record `tools/check_all.py` writes for a full, green sweep
    started from a clean tree at `sha` (#738). None when there is none, or when
    it cannot be read, which for the gate means the same thing."""
    record_dir = pathlib.Path(os.environ.get("SWEEP_RECORD_DIR", "/tmp/ai-terminal-sweep"))
    try:
        record = json.loads((record_dir / f"{sha.lower()}.json").read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None

# Gate 3, added 2026-09-14 on the owner's ruling. `Fail-before` was prose in
# implementer-brief.md and in the shared skill from the start, and prose is what
# it stayed: measured across the last 25 merged PRs, `Sweep SHA` — which this
# hook refuses a merge over — was present in 24, and `Fail-before` in 7. Same
# week, same authors, same loop. The difference was whether something refused.
#
# The required form, in the PR body, one line:
#
#     - **Fail-before:** tools/screensaver_check.py 367 -> 368
#
# Two passing-case counts from the owning script: at the base, then at the
# branch. The second must exceed the first, which is what makes it a statement
# that a case went red before the fix and green after.
FAIL_BEFORE_PATTERN = re.compile(
    r"(?i)\bFail[\s-]*before\b[*`:\s]*`?([\w./-]+\.py)`?"
    r"[*`:\s]*(\d+)\s*(?:->|→|to)\s*(\d+)"
)

# The escape hatch, and it is a real hole: a waiver is a line somebody types.
# It is here because the alternative is worse — a gate with no waiver wedges the
# loop on the issues that genuinely cannot carry a case, and a wedged gate gets
# switched off, which is how a hook becomes prose again. The reason has to be one
# of the four the shared skill's *The reproduction gate* enumerates, so the
# waiver is greppable and the monitor audits its use each tick.
FAIL_BEFORE_WAIVER = re.compile(
    r"(?i)\bFail[\s-]*before\b[*`:\s]*waived\s*[-–—:]\s*"
    r"(needs:\s*tester\s+repro|loop/process|docs\s+only|owner\s+ruling)\b"
)

CHECK_SCRIPT = re.compile(r"(?:^|/)tools/[\w-]*_check\.py$")

# The counts above are typed by the author, so they are a claim like any other.
# This one is not: every one of the 22 suites states its cases as a `check(` call
# at the start of a line, so counting them at `origin/main` and at the PR head
# measures whether a case was added without asking anybody. It is what separates
# a new case from a rewritten expectation, which is the #632 shape — +15/-14 in
# `screensaver_check.py`, 367 `check(` calls before and 367 after, one existing
# assertion inverted to match the new behaviour, merged, reopened 13 minutes
# later. Additions alone cannot see that: 24 of the last 25 merged PRs add lines
# to a check script, #632 among them.
CHECK_CASE = re.compile(r"^[ \t]*check\(", re.MULTILINE)


def check_case_count(ref: str, path: str) -> int | None:
    """`check(` cases in one suite at one ref, or None if the blob is unreadable."""
    content = get_file_content_at_commit(ref, path)
    return None if content is None else len(CHECK_CASE.findall(content))


def merge_base(base: str, head: str) -> str | None:
    """Where the branch actually forked, or None if this checkout cannot say.

    Not the base tip. The loop fans out up to ten implementers at once, so by the
    time one merges, `baseRefOid` has moved past the point its branch left.
    Measured across the last 25 merged PRs: comparing against the base tip makes
    #618-#621 each look like they deleted six to eight cases from
    `deploy_check.py`, when what they did was branch before those cases existed.
    Against the fork point the same 25 give two refusals instead of ten, and one
    of the two is #632.

    None when the objects are not here to compare — an unfetched branch is not
    evidence of anything, and a gate that refuses on missing data is a gate
    somebody turns off.
    """
    if not base or not head:
        return None
    try:
        proc = subprocess.run(
            ["git", "merge-base", base, head],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return None


def get_file_content_at_commit(head_sha: str, path: str) -> str | None:
    """Read file content at a specific commit SHA using git show."""
    if not head_sha or not path:
        return None
    clean_path = path.lstrip("./")
    try:
        proc = subprocess.run(
            ["git", "show", f"{head_sha}:{clean_path}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout
    except Exception:
        pass

    # If git show failed because the commit isn't local, try fetching from origin
    try:
        subprocess.run(
            ["git", "fetch", "origin", head_sha],
            capture_output=True,
            timeout=8,
        )
        proc = subprocess.run(
            ["git", "show", f"{head_sha}:{clean_path}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout
    except Exception:
        pass

SHARED_REPO_DEFAULT = str(pathlib.Path(__file__).resolve().parents[2])


def get_shared_repo_dir() -> str:
    """Return the path to the shared checkout, overridable by SHARED_REPO_PATH for tests."""
    repo = os.environ.get("SHARED_REPO_PATH", "").strip()
    if repo:
        return repo
    if os.path.isdir(os.path.join(SHARED_REPO_DEFAULT, ".git")):
        return SHARED_REPO_DEFAULT
    return str(pathlib.Path(__file__).resolve().parents[2])


def get_check_script_names(head_sha: str) -> list[str]:
    """Return sorted list of check suite names (e.g. ['agenda', 'wiring']) at head_sha."""
    if head_sha:
        try:
            proc = subprocess.run(
                ["git", "ls-tree", "--full-tree", "-r", "--name-only", head_sha],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0:
                names = []
                for line in proc.stdout.splitlines():
                    line = line.strip()
                    if CHECK_SCRIPT.search(line):
                        fname = os.path.basename(line)
                        names.append(fname.removesuffix("_check.py"))
                return sorted(set(names))
        except Exception:
            pass
        return []

    # Fallback to local disk candidate search only if head_sha is not provided
    import glob
    candidates = [
        "tools/*_check.py",
        "2026-0906-raspi-ai-terminal/tools/*_check.py",
    ]
    for cand in candidates:
        found = glob.glob(cand)
        if found:
            return sorted(set(os.path.basename(p).removesuffix("_check.py") for p in found))
    return []


def count_check_scripts(head_sha: str) -> int:
    """Count tools/*_check.py in the repository tree at head_sha, or on disk."""
    if head_sha:
        return len(get_check_script_names(head_sha))

    import glob
    candidates = [
        "tools/*_check.py",
        "2026-0906-raspi-ai-terminal/tools/*_check.py",
    ]
    try:
        toplevel = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        if toplevel:
            candidates.insert(0, os.path.join(toplevel, "2026-0906-raspi-ai-terminal/tools/*_check.py"))
            candidates.insert(1, os.path.join(toplevel, "tools/*_check.py"))
    except Exception:
        pass

    hook_repo = str(pathlib.Path(__file__).resolve().parents[2])
    candidates.append(os.path.join(hook_repo, "2026-0906-raspi-ai-terminal/tools/*_check.py"))
    candidates.append(os.path.join(hook_repo, "tools/*_check.py"))

    repo_dir = get_shared_repo_dir()
    if repo_dir:
        candidates.append(os.path.join(repo_dir, "2026-0906-raspi-ai-terminal/tools/*_check.py"))
        candidates.append(os.path.join(repo_dir, "tools/*_check.py"))

    for pattern in candidates:
        matches = glob.glob(pattern)
        if matches:
            return len(matches)
    return 0


def check_shared_checkout_clean() -> None:
    """Refuse merge if shared checkout is dirty or local main is ahead of origin/main (#582)."""
    repo_dir = get_shared_repo_dir()
    if not os.path.exists(repo_dir):
        return

    # Check 1: git status --porcelain must be empty
    try:
        proc_status = subprocess.run(
            ["git", "-C", repo_dir, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc_status.returncode == 0 and proc_status.stdout.strip():
            dirty = proc_status.stdout.strip()
            block(
                f"Shared repository checkout at `{repo_dir}` is dirty.\n\n"
                f"Unreviewed writes or untracked changes were detected:\n"
                f"```\n{dirty}\n```\n\n"
                "The coder cannot merge while unreviewed edits exist in the shared checkout (#582).\n"
                "Implementing subagents must work strictly in `/tmp/rev-<N>-wt` worktrees.\n"
                "Revert, stash, or clean unreviewed changes in the shared checkout before merging."
            )
    except Exception:
        pass

    # Check 2: git rev-list origin/main..main must be empty
    try:
        proc_rev = subprocess.run(
            ["git", "-C", repo_dir, "rev-list", "origin/main..main"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc_rev.returncode == 0 and proc_rev.stdout.strip():
            commits = proc_rev.stdout.strip()
            block(
                f"Local `main` at `{repo_dir}` has unpushed stray commits ahead of `origin/main`.\n\n"
                f"Unpushed commits detected:\n"
                f"```\n{commits}\n```\n\n"
                "The coder cannot merge while stray commits exist on local `main` (#582).\n"
                "Unreviewed commits written directly to local main bypass the pull request review loop.\n"
                "Reset or clean local `main` to `origin/main` before merging."
            )
    except Exception:
        pass


def check_pr_merge(command: str) -> None:
    """Pre-merge gates: clean shared checkout, AST parse, conflict markers, and sweep SHA.

    Gate 0: Verify shared checkout is clean (`git status --porcelain` is empty) and
    local main has no unpushed stray commits ahead of origin/main (`git rev-list
    origin/main..main` is empty). Catches direct writes to shared checkout (#582).

    Gate 1: Verify all changed .py files at PR head pass ast.parse and contain
    no git conflict markers (^(<{7}|={7}|>{7})). Catches hand-resolved conflict
    defects like #562.

    Gate 2: Extract `Sweep SHA` from PR body and assert head_sha.startswith(sweep_sha).
    Refuse merge if missing or if head moved since sweep (e.g. after a rebase).
    Catches stale sweep claims like #560.

    Gate 3: Require `- **Fail-before:** tools/<name>_check.py <N> -> <M>` with
    M > N, or an explicit waiver, and check the claim by counting `check(` calls
    at the fork point and at the head. Catches the #632 shape.

    Gate 4: Refuse a merge that would auto-close an issue labelled `needs:live`.
    Such an issue's acceptance is only observable on the Pi or against a live
    model, so the pull request writes `Refs #N` and the close waits for the
    observation. Catches verification that never happens because the issue left
    the queue at the merge.
    """
    check_shared_checkout_clean()
    repo_match = REPO_FLAG.search(command)
    repo = ""
    if repo_match:
        repo = next(g for g in repo_match.groups()[1:] if g is not None)

    prs: list[str] = []
    if MERGE.search(command):
        segment = MERGE_SEGMENT.search(command)
        if segment:
            found = BARE_NUMBER.search(segment.group(1))
            prs.append(found.group(1) if found else "")
        prs += REST_PR_TARGET.findall(command)

    for pr in prs[:3]:
        view = ["gh", "pr", "view"]
        if pr:
            view.append(pr)
        data = gh_json(
            view
            + ["--json", "headRefOid,baseRefOid,body,files,closingIssuesReferences"],
            repo,
        )
        if not isinstance(data, dict):
            continue

        head_sha = str(data.get("headRefOid") or "")
        base_sha = str(data.get("baseRefOid") or "") or "origin/main"
        body = str(data.get("body") or "")
        files = data.get("files") or []

        # Gate 4: a merge must not close an issue whose acceptance is only
        # observable live. `closingIssuesReferences` is populated from `Closes
        # #N` and from nothing else, so a non-empty answer here is GitHub
        # confirming the auto-close, not a guess about the body's wording.
        closing = [
            str(ref["number"])
            for ref in (data.get("closingIssuesReferences") or [])[:10]
            if isinstance(ref, dict) and ref.get("number")
        ]
        live = [n for n in closing if LIVE in (issue_labels(n, repo) or set())]
        if live:
            which = ", ".join(f"#{n}" for n in live)
            block(
                f"{which} carries `{LIVE}`, and this merge would close it.\n\n"
                "That label says the acceptance criterion cannot be settled by any "
                "`tools/*_check.py` — it needs the Pi or a live model. An issue like "
                "that has to stay open past the merge, because closing it is what "
                "takes it out of everyone's queue: of 178 issues closed here, 171 "
                "closed within ninety seconds of their merge, and every rule about "
                "verifying afterwards is a rule about work nobody is holding.\n\n"
                f"Change `Closes #{live[0]}` to `Refs #{live[0]}` in the pull request "
                "body, then merge. The issue stays open, the coder swaps "
                f"`stage:pr` for `{LIVE_STAGE}`, and it closes when somebody posts a "
                "comment with a line reading\n\n"
                "```\nlive-verified: raspberrypi.local 2026-09-14\n```\n\n"
                "naming where the behaviour was actually seen.\n\n"
                "If the label is wrong — the acceptance really is checkable from a "
                "suite — say so on the issue. The monitor clears it; a merge does not."
            )

        # Gate 1: Check AST parse and conflict markers on changed .py files of PR head commit
        changed_py: list[str] = []
        if isinstance(files, list):
            for f in files:
                if isinstance(f, dict):
                    p = str(f.get("path") or "")
                    if p.endswith(".py") and f.get("changeType") != "DELETED":
                        changed_py.append(p)

        if not changed_py and head_sha:
            try:
                proc = subprocess.run(
                    ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", head_sha],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if proc.returncode == 0:
                    for line in proc.stdout.splitlines():
                        line = line.strip()
                        if line.endswith(".py"):
                            changed_py.append(line)
            except Exception:
                pass

        for p in changed_py:
            content = get_file_content_at_commit(head_sha, p)
            if content is None:
                continue
            m = CONFLICT_MARKER.search(content)
            if m:
                block(
                    f"Conflict marker `{m.group(1)}` found in `{p}` at PR head `{head_sha[:7]}`.\n\n"
                    "A hand-resolved conflict left a marker in the file (#562). "
                    "Resolve the conflict markers and update the PR before merging."
                )
            try:
                ast.parse(content, filename=p)
            except SyntaxError as e:
                block(
                    f"Syntax error in `{p}` at PR head `{head_sha[:7]}` (line {e.lineno}): {e.msg}.\n\n"
                    "The Python file fails `ast.parse`. Fix syntax errors before merging."
                )

        # Gate 2: Extract Sweep SHA from PR body and assert head_sha.startswith(sweep_sha)
        m_sweep = SWEEP_SHA_PATTERN.search(body)
        if not m_sweep:
            target = f"PR #{pr}" if pr else "PR"
            block(
                f"{target} body is missing `Sweep SHA`.\n\n"
                "Every PR body must record `- **Sweep SHA:** <HEAD sha>` to verify that "
                "the full check suite was measured against the head being merged.\n\n"
                "Run the sweep at the current head and add `Sweep SHA:` to the PR body."
            )

        sweep_sha = m_sweep.group(1).lower()
        if not head_sha.lower().startswith(sweep_sha):
            target = f"PR #{pr}" if pr else "PR"
            block(
                f"{target} head `{head_sha[:7]}` does not match Sweep SHA `{sweep_sha[:7]}`.\n\n"
                "The PR's head commit has moved since the sweep was measured (e.g. from a "
                "rebase or force push, as in #560). A rebase invalidates the sweep measurement.\n\n"
                "Re-run the sweep at the current head and update `Sweep SHA:` in the PR "
                "body before merging."
            )

        # Gate 2: Sweep provenance and completeness (#655)
        target = f"PR #{pr}" if pr else "PR"
        if not SWEEP_WAIVER.search(body):
            expected_suites = count_check_scripts(head_sha)
            expected_names = get_check_script_names(head_sha)
            m_breakdown = SWEEP_LINE_PATTERN.search(body)
            if m_breakdown:
                stated_total = int(m_breakdown.group(2))
                breakdown_str = m_breakdown.group(3)
                pairs = re.findall(r"([a-zA-Z0-9_-]+)\s+(\d+)", breakdown_str)
                actual_sum = sum(int(n) for _, n in pairs)
                if stated_total != actual_sum:
                    diff = actual_sum - stated_total
                    diff_s = f"+{diff}" if diff > 0 else f"{diff}"
                    block(
                        f"{target} sweep breakdown sum does not match stated total: "
                        f"`{stated_total} green:` vs sum of suites `{actual_sum}` ({diff_s}).\n\n"
                        "The stated total must equal the sum of the per-suite check counts.\n"
                        "Paste `tools/check_all.py`'s literal `green:` line into the PR body "
                        "rather than retyping it (#655)."
                    )
                if expected_suites > 0 and len(pairs) < expected_suites:
                    block(
                        f"{target} sweep reports {len(pairs)} check suites, but repository has "
                        f"{expected_suites} suites at PR head `{head_sha[:7]}`.\n\n"
                        f"PR reported only {len(pairs)} of {expected_suites} check suites without a waiver. "
                        "Every PR must run the full check suite sweep (or provide an explicit waiver):\n\n"
                        "    - **Sweep:** waived - loop/process\n"
                        "    - **Sweep:** waived - docs only\n"
                        "    - **Sweep:** waived - owner ruling\n\n"
                        "Run `tools/check_all.py` and paste the literal `green:` line into the PR body."
                    )
                reported_names = sorted(s for s, _ in pairs)
                if expected_names and reported_names != expected_names:
                    missing = set(expected_names) - set(reported_names)
                    unknown = set(reported_names) - set(expected_names)
                    errs = []
                    if missing:
                        errs.append(f"missing suites: {', '.join(sorted(missing))}")
                    if unknown:
                        errs.append(f"unknown suites: {', '.join(sorted(unknown))}")
                    block(
                        f"{target} sweep breakdown suites do not match repository check scripts at `{head_sha[:7]}`: "
                        f"{'; '.join(errs)}.\n\n"
                        "Every check suite present in `tools/*_check.py` must be accounted for (#689)."
                    )
                # The line above is typed into the body; the record is written by
                # the sweep itself. #741, #743 and #747 each gave their head as
                # `Sweep SHA` over a sweep run on a dirty tree before the commit.
                record = load_sweep_record(head_sha)
                if record is None:
                    block(
                        f"{target} has no sweep record for its head `{head_sha[:7]}`.\n\n"
                        "`tools/check_all.py` writes `/tmp/ai-terminal-sweep/<sha>.json` only "
                        "for a full, green sweep started from a clean tree, so the `green:` "
                        "line in the body was not measured at this commit (#738).\n\n"
                        "Commit first, then run the full sweep at that commit (its `git:` "
                        "line must read `(clean)`, and it prints `sweep record: <path>`), "
                        "paste its `green:` line, and push."
                    )
                rec_areas = record.get("areas") or {}
                stated_areas = {s: int(n) for s, n in pairs}
                if record.get("total") != stated_total or rec_areas != stated_areas:
                    diffs = [
                        f"{s} {stated_areas.get(s, '-')} vs {rec_areas.get(s, '-')}"
                        for s in sorted(set(stated_areas) | set(rec_areas))
                        if stated_areas.get(s) != rec_areas.get(s)
                    ]
                    block(
                        f"{target} sweep line does not match the sweep record for `{head_sha[:7]}`: "
                        f"body `{stated_total} green:` vs record `{record.get('total')}`"
                        + (f"; {', '.join(diffs)}" if diffs else "") + ".\n\n"
                        "Paste the `green:` line from the sweep that ran at this head (#738)."
                    )
            else:
                m_count = SWEEP_COUNT_PATTERN.search(body)
                if m_count:
                    block(
                        f"{target} reports summary count `{m_count.group(0)}` but is missing the full suite breakdown.\n\n"
                        "Every PR body must record the full check suite breakdown from `tools/check_all.py`:\n\n"
                        "    <total> green: <suite1> <n1> · <suite2> <n2> · ...\n\n"
                        "Paste `tools/check_all.py`'s literal `green:` line into the PR body (#689)."
                    )
                elif expected_suites > 0:
                    block(
                        f"{target} body is missing check suite sweep or valid waiver.\n\n"
                        f"Repository has {expected_suites} check suites at PR head `{head_sha[:7]}`. "
                        "Every PR body must record the full check suite sweep:\n\n"
                        "    <total> green: <suite1> <n1> · <suite2> <n2> · ...\n\n"
                        "Or an explicit waiver:\n\n"
                        "    - **Sweep:** waived - loop/process\n"
                        "    - **Sweep:** waived - docs only\n"
                        "    - **Sweep:** waived - owner ruling\n\n"
                        "Run `tools/check_all.py` and paste the literal `green:` line into the PR body."
                    )

        # Gate 3: the fix must be pinned by a check case that was red before it.
        if FAIL_BEFORE_WAIVER.search(body):
            continue

        m_fb = FAIL_BEFORE_PATTERN.search(body)
        if not m_fb:
            block(
                f"{target} body is missing `Fail-before`.\n\n"
                "Every PR body must record one line:\n\n"
                "    - **Fail-before:** tools/<name>_check.py <passing at base> -> "
                "<passing at branch>\n\n"
                "Run the owning script against the unpatched base and against your "
                "branch, and put both counts in. The second must be higher: that is "
                "what says a case went red before the fix and green after.\n\n"
                "If no case can be written, say which of the four situations this is "
                "and waive it explicitly:\n\n"
                "    - **Fail-before:** waived - needs: tester repro\n"
                "    - **Fail-before:** waived - loop/process\n"
                "    - **Fail-before:** waived - docs only\n"
                "    - **Fail-before:** waived - owner ruling\n\n"
                "See *The reproduction gate* in "
                ".claude/skills/issue-looping-shared/SKILL.md. Owner's rule, "
                "2026-09-14."
            )

        script, before_s, after_s = m_fb.group(1), m_fb.group(2), m_fb.group(3)
        if int(after_s) <= int(before_s):
            block(
                f"{target} `Fail-before` does not show a case going green: "
                f"`{script} {before_s} -> {after_s}`.\n\n"
                "The count at the branch must exceed the count at the base. Equal "
                "counts mean the fix is pinned by nothing, which is #632: a change "
                "that shipped with 368 cases before and 368 after, reported 22/22 "
                "green, and was reopened thirteen minutes after the merge.\n\n"
                "Write a case that reproduces the defect, show it red at the base, "
                "and update the line."
            )

        touched_checks = [
            str(f.get("path") or "")
            for f in (files if isinstance(files, list) else [])
            if isinstance(f, dict)
            and CHECK_SCRIPT.search(str(f.get("path") or ""))
            and int(f.get("additions") or 0) > 0
        ]
        if not touched_checks:
            block(
                f"{target} claims `{script} {before_s} -> {after_s}` but adds no "
                "lines to any `tools/*_check.py`.\n\n"
                "A count that rose without the check file growing means the case "
                "was already there and its expected value was rewritten. That is "
                "not a fix; it is the issue's `Acceptance` being renegotiated "
                "inside a diff, where nobody reviews it — PR #632, and #488 and "
                "#580 before it.\n\n"
                "Add the case. If you believe the existing expectation is wrong, "
                "stop and say so on the issue: that is the owner's call."
            )

        # Count the cases rather than the lines. Unreadable either side means the
        # branch is not fetched here, and a gate that cannot see is a gate that
        # says nothing — the sweep gate above treats a missing blob the same way.
        grew = False
        measured = False
        deltas: list[str] = []
        fork_sha = merge_base(base_sha, head_sha)
        for path in touched_checks if fork_sha else []:
            before_n = check_case_count(fork_sha, path)
            after_n = check_case_count(head_sha, path)
            if before_n is None or after_n is None:
                continue
            measured = True
            deltas.append(f"`{path}` {before_n} -> {after_n}")
            if after_n > before_n:
                grew = True

        if measured and not grew:
            block(
                f"{target} adds lines to a check script but no case: "
                + ", ".join(deltas)
                + f".\n\nCounting `check(` calls at the fork point `{(fork_sha or '')[:7]}` and "
                f"at the PR head `{head_sha[:7]}`, the number did not rise. Lines changed "
                "and cases did not, which means an existing assertion was "
                "rewritten to match the new behaviour. That is the #632 shape: "
                "367 cases before, 367 after, one expected value inverted in the "
                "same diff as the change, 22/22 green, reopened by the owner "
                "thirteen minutes after the merge.\n\n"
                "Write the case that reproduces the defect and show it red at the "
                "base. If you believe an existing expectation is wrong, stop and "
                "say so on the issue — that is the owner's call, not a diff."
            )


def is_draft_pr(command: str) -> bool:
    """Return True if command includes --draft or -d flag."""
    try:
        tokens = shlex.split(command)
        return any(t in ("--draft", "-d") for t in tokens)
    except ValueError:
        return bool(DRAFT_FLAG.search(command))


def extract_pr_create_body(command: str) -> str | None:
    """Extract PR body from --body-file, -F, --body, or -b flags."""
    m_file = PR_BODY_FILE.search(command)
    if m_file:
        path = next(g for g in (m_file.group(2), m_file.group(3), m_file.group(4)) if g is not None)
        if path == "-":
            return None
        try:
            return pathlib.Path(path).read_text(errors="replace")
        except OSError:
            return None

    try:
        tokens = shlex.split(command)
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in ("--body", "-b") and i + 1 < len(tokens):
                return tokens[i + 1]
            if tok.startswith(("--body=", "-b=")):
                return tok.split("=", 1)[1]
            i += 1
    except ValueError:
        pass

    m_body = CLI_BODY_FLAG.search(command)
    if m_body:
        return next(g for g in (m_body.group(1), m_body.group(2), m_body.group(3)) if g is not None)

    return None


def check_pr_create(command: str) -> None:
    """Pre-creation gates: draft carve-out, Sweep SHA, and Fail-before verification.

    Draft carve-out: If --draft or -d is present, allow without Gate 2/3 checks.
    Gate 2: Must contain `- **Sweep SHA:** <HEAD sha>`. Refuse if missing.
    Gate 3: Must contain `- **Fail-before:** tools/<name>_check.py <N> -> <M>`
    or one of the 4 valid waivers. Refuse if missing.
    """
    if is_draft_pr(command):
        return

    body = extract_pr_create_body(command)
    if body is None:
        return

    m_sweep = SWEEP_SHA_PATTERN.search(body)
    if not m_sweep:
        block(
            "PR body is missing `Sweep SHA`.\n\n"
            "Every PR body must record `- **Sweep SHA:** <HEAD sha>` to verify that "
            "the full check suite was measured against the head commit being submitted.\n\n"
            "Run the sweep at the current head and add `- **Sweep SHA:** <HEAD sha>` to the PR body."
        )

    if FAIL_BEFORE_WAIVER.search(body):
        return

    m_fb = FAIL_BEFORE_PATTERN.search(body)
    if not m_fb:
        block(
            "PR body is missing `Fail-before`.\n\n"
            "Every PR body must record one line:\n\n"
            "    - **Fail-before:** tools/<name>_check.py <passing at base> -> "
            "<passing at branch>\n\n"
            "Run the owning script against the unpatched base and against your "
            "branch, and put both counts in. The second must be higher: that is "
            "what says a case went red before the fix and green after.\n\n"
            "If no case can be written, say which of the four situations this is "
            "and waive it explicitly:\n\n"
            "    - **Fail-before:** waived - needs: tester repro\n"
            "    - **Fail-before:** waived - loop/process\n"
            "    - **Fail-before:** waived - docs only\n"
            "    - **Fail-before:** waived - owner ruling\n\n"
            "See *The reproduction gate* in "
            ".claude/skills/issue-looping-shared/SKILL.md. Owner's rule, "
            "2026-09-14."
        )

    script, before_s, after_s = m_fb.group(1), m_fb.group(2), m_fb.group(3)
    if int(after_s) <= int(before_s):
        block(
            f"PR `Fail-before` does not show a case going green: "
            f"`{script} {before_s} -> {after_s}`.\n\n"
            "The count at the branch must exceed the count at the base. Equal "
            "counts mean the fix is pinned by nothing, which is #632: a change "
            "that shipped with 368 cases before and 368 after, reported 22/22 "
            "green, and was reopened thirteen minutes after the merge.\n\n"
            "Write a case that reproduces the defect, show it red at the base, "
            "and update the line."
        )


def main(raw: str) -> None:
    payload = json.loads(raw)
    command, session_id = read_call(payload)
    role = resolve_role(session_id)
    record_heartbeat(session_id, role, command)

    if not command:
        call = payload.get("toolCall") if isinstance(payload, dict) else None
        if isinstance(call, dict) and call.get("name") and call.get("name") != "run_command":
            allow()

        if CC_PAYLOAD.search(raw) or AGY_PAYLOAD.search(raw):
            block(
                "Unparseable command execution payload from recognized host: "
                "the guard could not extract a command to inspect. "
                "Refusing rather than failing open (#643)."
            )
        allow()

    # Before the write test, because this one is not a `gh` write.
    secret_check(command)

    if not any(re.search(p, command) for p in WRITE_PATTERNS):
        allow()

    if role not in ROLES:
        block(
            "This session has no role, so it may not write to GitHub.\n\n"
            "The loop has four roles — monitor, reviewer, coder, tester — and which "
            "one you are decides what you may label and how your comments are signed. "
            "Inferring it from SKILL.md is what this guard exists to stop: on "
            "2026-09-10 a session started with `read this project` did exactly that and "
            "filed #155 unsigned, with a reviewer-only label.\n\n"
            "There is a fifth, `aide`, for a session the owner started outside the loop "
            "to write something down. It files and comments, signs `<!-- aide -->`, and "
            "may not close or move an issue's stage. If that is what this session is, "
            "the owner assigns it the same way as any other role.\n\n"
            "Do not pick a role. Ask the owner which one this session is, then either "
            "relaunch with `.claude/hooks/ccrole.sh <role>` or have them run "
            f"`.claude/hooks/ccrole.sh assign {session_id or '<session-id>'} <role>`.\n\n"
            "Reads are unaffected — you can keep investigating and report what you found."
        )

    if MERGE.search(command) and role != "coder":
        block(
            f"Only the coder merges, and this session is the {role}.\n\n"
            "Since 2026-09-11 the reviewer writes the code, through implementing "
            "subagents that each open a pull request. `gh pr merge` is what still "
            "keeps the session that writes apart from the session that lands: the "
            "coder reads the diff before merging, and that is the only independent "
            "look anything gets.\n\n"
            "Open the PR, put `stage:pr` on the issue, and leave it. The coder picks "
            "it up from `gh pr list --state open`."
        )

    if CLOSES.search(command) and role in ("tester", "aide"):
        block(
            f"The {role} files and comments; it does not close.\n\n"
            "You see one failing page, or one thing the owner asked you to write "
            "down — not the queue. Whether the issue behind it is fixed, duplicated, "
            "overtaken or not worth doing is a judgement made from the backlog, and "
            "that is the monitor's and the reviewer's.\n\n"
            "Say it on the thread and leave the state alone."
        )

    marker = ROLES[role]
    if any(re.search(p, command) for p in BODY_PATTERNS):
        has_body, is_signed = cli_comment_status(command, marker)
        if not has_body:
            refuse_unreadable_body(role)
        if not is_signed:
            refuse_unsigned_comment(role, marker)

    is_comment, body = rest_comment_body(command)
    if is_comment:
        if body is None:
            refuse_unreadable_body(role)
        if not body.lstrip("\ufeff \t\r\n").startswith(marker):
            refuse_unsigned_comment(role, marker)

    added, removed = labels_in(command)
    for label in sorted(added):
        rule = LABEL_RULES.get(label)
        if rule and role not in rule["add"]:
            who = ", ".join(sorted(rule["add"])) if rule["add"] else "no session — this is the owner's"
            block(
                f"`{label}` may not be applied by the {role}: that label belongs to "
                f"{who} — see the ownership table in issue-looping-shared/SKILL.md.\n\n"
                "If you need the effect it would have had, say so on the thread and ask. "
                "For keeping an issue out of the pick order, the correct instrument is an "
                "issue dependency, which any role may record and which says what lifts it."
            )
    for label in sorted(removed):
        rule = LABEL_RULES.get(label)
        if rule and role not in rule["remove"]:
            who = ", ".join(sorted(rule["remove"])) if rule["remove"] else "no session — this is the owner's"
            block(
                f"`{label}` may not be cleared by the {role}: clearing it belongs to "
                f"{who} — see the ownership table in issue-looping-shared/SKILL.md.\n\n"
                "Clearing another role's label overrules a judgement you cannot see the "
                "reasons for. Ask on the thread instead."
            )

    stage_added = {label for label in added if label.startswith("stage:")}
    if len(stage_added) > 1:
        block(
            f"Cannot apply multiple stage labels at once ({', '.join(sorted(stage_added))}).\n\n"
            "An issue may carry exactly one `stage:` label at any moment."
        )

    if "stage:pr" in added:
        missing = STAGE_PR_REQUIRED_CLEARS - removed
        if missing:
            block(
                "Advancing an issue to `stage:pr` must clear prior stage labels "
                f"({', '.join(sorted(STAGE_PR_REQUIRED_CLEARS))}).\n\n"
                f"Missing from remove: {', '.join(sorted(missing))}.\n\n"
                "An issue may carry exactly one `stage:` label at any moment. The triage -> "
                "pr shortcut leaves stage:triage set if only stage:ready is cleared (#333). "
                "Include `--remove-label stage:ready,stage:triage,stage:owner` in the command."
            )

    # Last, and only on a close or a merge, because it is the one rule that costs
    # a network round trip. A command that lifts the hold in the same breath is
    # the sanctioned way through: `gh issue edit N --remove-label hold:monitor &&
    # gh issue close N` reads the label as still set, and refusing that would
    # refuse the monitor's own way of ending a hold.
    if HOLD not in removed and (CLOSES.search(command) or MERGE.search(command)):
        held = held_issues(command)
        if held:
            refuse_held(role, held)

    if MERGE.search(command):
        check_pr_merge(command)

    if CLOSES.search(command) and LIVE not in removed:
        check_issue_close(command)

    if PR_CREATE.search(command):
        check_pr_create(command)

    allow()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check-alive":
        session_arg = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else None
        sys.exit(check_alive(session_id=session_arg))

    # Read stdin and classify the host before anything that can throw, so the
    # handler below answers in that host's vocabulary rather than in whichever
    # one the module happened to default to.
    try:
        RAW = sys.stdin.read()
    except Exception:  # noqa: BLE001
        RAW = ""
    DENY = sniff_deny(RAW)

    try:
        main(RAW)
    except Exception as exc:  # noqa: BLE001
        note = f"role_guard error, allowing: {exc}"
        if DENY == "deny":
            # Bare `decision`, and the note goes to stderr. agy's PreToolHookResult
            # carries decision, reason, overwrite, permission_overrides, allow_tool
            # and deny_reason — there is no `systemMessage` on it. (The binary does
            # contain that string, which is why this looks wrong at a glance: it
            # belongs to `HookSystemMessage` and `HookInjectedStep`, which are a
            # different message. Checked against the getters on the result type.)
            # The binary also carries `DisallowUnknownFields`, so an undeclared
            # field risks the object being rejected outright — inside the error
            # handler, which would turn the fail-open into a hard failure. stderr
            # costs nothing and agy already surfaces it. #277.
            print(json.dumps({"decision": "allow"}))
        else:
            # Claude Code does declare `systemMessage`, so it keeps it.
            print(json.dumps({"systemMessage": note}))
        print(note, file=sys.stderr)
