#!/usr/bin/env python3
"""Cases for role_guard.py. Run it: `uv run --no-project python role_guard_test.py`.

The guard had no tests until 2026-09-11, and by then it had shipped two defects in
one day: `allow()` printed a bare `{}`, which the Antigravity CLI reads as a refusal
and which blocked the coder on its own first command; and the fail-open handler has
the same shape (#268). Both are one assertion each. Hence this file.

It drives the guard as a subprocess with a payload on stdin, exactly as a host does,
so it tests the wire contract rather than the functions. Two hosts answer differently
for the same verdict and getting that mapping wrong is the whole of the first defect,
so `run()` returns a plain "allow"/"refuse" and the host difference is asserted
separately in `test_host_shapes`.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import uuid

GUARD = pathlib.Path(__file__).with_name("role_guard.py")

CLAUDE, AGY = "claude", "agy"

# Isolate all tests from production /tmp/role_guard.last and /tmp/role_guard_runs (#656)
TEST_TMPDIR = tempfile.TemporaryDirectory(prefix="role_guard_test_")
TEST_HEARTBEAT = pathlib.Path(TEST_TMPDIR.name) / "role_guard.last"
TEST_RUNS = pathlib.Path(TEST_TMPDIR.name) / "role_guard_runs"
os.environ["ROLE_GUARD_HEARTBEAT_FILE"] = str(TEST_HEARTBEAT)
os.environ["ROLE_GUARD_RUNS_DIR"] = str(TEST_RUNS)
# ...and from the real sweep records in /tmp/ai-terminal-sweep (#733, #738)
TEST_SWEEP_RECORDS = pathlib.Path(TEST_TMPDIR.name) / "sweep_records"
os.environ["SWEEP_RECORD_DIR"] = str(TEST_SWEEP_RECORDS)


def write_sweep_record(sha: str, green_line: str) -> None:
    """The record check_all.py would have written for `green_line` at `sha`."""
    total, rest = green_line.split(" green:", 1)
    areas = {s: int(n) for s, n in re.findall(r"([a-zA-Z0-9_-]+)\s+(\d+)", rest)}
    TEST_SWEEP_RECORDS.mkdir(parents=True, exist_ok=True)
    (TEST_SWEEP_RECORDS / f"{sha}.json").write_text(
        json.dumps({"sha": sha, "areas": areas, "total": int(total)}))


def run(
    command: str,
    role: str = "monitor",
    host: str = CLAUDE,
    extra_env: dict | None = None,
) -> tuple[str, str]:
    """Returns (verdict, reason), verdict in {"allow", "refuse"}."""
    if host == CLAUDE:
        payload = {"tool_input": {"command": command}, "session_id": "test-session"}
    else:
        payload = {
            "toolCall": {"name": "run_command", "args": {"CommandLine": command}},
            "conversationId": "test-conversation",
        }
    env = dict(os.environ)
    if role:
        env["CC_ROLE"] = role
    else:
        env.pop("CC_ROLE", None)
    env.update(extra_env or {})
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    out = json.loads(proc.stdout or "{}")
    decision = out.get("decision", "")
    verdict = "refuse" if decision in ("block", "deny") else "allow"
    return verdict, out.get("reason", "")


FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     want: {want!r}\n      got: {got!r}")


# --------------------------------------------------------------------------- #
# The /api/config trap (#270)
#
# GET /api/config returns maps_api_key in the clear, because the browser needs it
# for Maps JS. On 2026-09-10 the monitor fetched it unredacted; on 2026-09-11 the
# coder did the same twice and put a live key into its own conversation store. Both
# roles had read the paragraph in SKILL.md that names this exact endpoint. The rule
# binds every role, so every case here is asserted for all three.
# --------------------------------------------------------------------------- #


def test_config_endpoint() -> None:
    for role in ("monitor", "reviewer", "coder"):
        verdict, reason = run("curl -s http://localhost:8000/api/config", role=role)
        check(f"bare curl of /api/config is refused for {role}", verdict, "refuse")
        check(
            f"the refusal for {role} names the substitute",
            "sha256sum" in reason,
            True,
        )

    # Over ssh is the shape that actually happened, and the endpoint is inside a
    # quoted argument rather than at the top level of the command.
    verdict, _ = run("""ssh raspberrypi.local "curl -s localhost:8000/api/config" """, role="coder")
    check("curl inside an ssh argument is refused", verdict, "refuse")

    verdict, _ = run("wget -qO- http://raspberrypi.local:8000/api/config", role="reviewer")
    check("wget is refused too", verdict, "refuse")

    # jq is the trap inside the trap: pi2 does not have it, so the pipe fails after
    # curl has already printed the whole object. A redaction that fails open is
    # worse than none, so this is refused with its own reason rather than allowed.
    verdict, reason = run(
        """ssh raspberrypi.local "curl -s localhost:8000/api/config | jq 'del(.maps_api_key)'" """,
        role="reviewer",
    )
    check("a jq redaction is refused", verdict, "refuse")
    check("the jq refusal says jq is absent on pi2", "jq" in reason, True)


def test_config_endpoint_allows_the_safe_forms() -> None:
    # The form SKILL.md recommends: select field *names* and one non-secret value.
    safe = (
        """ssh raspberrypi.local 'curl -s localhost:8000/api/config | python3 -c """
        '''"import json,sys; d=json.load(sys.stdin); print(d[\\"revision\\"]); print(sorted(d))"' '''
    )
    verdict, _ = run(safe, role="reviewer")
    check("a python filter that selects field names is allowed", verdict, "allow")

    verdict, _ = run(
        "curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/api/config",
        role="reviewer",
    )
    check("a status-code probe that discards the body is allowed", verdict, "allow")

    # Reading the source is not fetching the endpoint, and the loop greps for this
    # path constantly. A rule that catches the grep would be turned off in an hour.
    verdict, _ = run("grep -rn '/api/config' src/", role="monitor")
    check("grepping the source for the path is allowed", verdict, "allow")

    # The scheme is part of what you grep for, so a fetcher list containing `http`
    # refuses the grep for containing its own URL. Found by writing this case.
    verdict, _ = run("grep -rn 'http://localhost:8000/api/config' docs/", role="monitor")
    check("grepping for the full URL is allowed", verdict, "allow")

    verdict, _ = run("curl -s http://localhost:8000/api/health", role="reviewer")
    check("another endpoint on the same host is allowed", verdict, "allow")


# --------------------------------------------------------------------------- #
# What the guard already did. These are here because nothing asserted them.
# --------------------------------------------------------------------------- #


def test_reads_are_untouched() -> None:
    for cmd in (
        "gh issue view 269",
        "gh issue list --label ai-terminal",
        "gh api repos/o/r/issues/1",
        "git status",
    ):
        verdict, _ = run(cmd, role="monitor")
        check(f"read is allowed: {cmd}", verdict, "allow")


def test_no_role_cannot_write() -> None:
    verdict, reason = run("gh issue close 1", role="")
    check("a session with no role may not write", verdict, "refuse")
    check("and is told to ask rather than to choose", "Ask the owner" in reason, True)

    verdict, _ = run("gh issue view 1", role="")
    check("but may still read", verdict, "allow")


def test_comments_need_the_marker() -> None:
    verdict, _ = run("gh issue comment 1 --body 'looks fine'", role="monitor")
    check("an unsigned comment is refused", verdict, "refuse")

    verdict, _ = run("gh issue comment 1 --body '<!-- monitor --> looks fine'", role="monitor")
    check("a signed comment is allowed", verdict, "allow")

    verdict, _ = run("gh issue comment 1 --body '<!-- reviewer --> looks fine'", role="monitor")
    check("someone else's marker is refused", verdict, "refuse")

    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write("<!-- monitor -->\nbody\n")
        path = fh.name
    verdict, _ = run(f"gh issue comment 1 --body-file {path}", role="monitor")
    check("the marker is found inside a --body-file", verdict, "allow")
    pathlib.Path(path).unlink()

    # A bare close carries no text, so it cannot carry a marker.
    verdict, _ = run("gh issue close 1 --reason 'not planned'", role="monitor")
    check("a bare close needs no marker", verdict, "allow")

    verdict, _ = run("gh issue close 1", role="monitor")
    check("bare close without comment is allowed for monitor", verdict, "allow")

    # Unreadable forms (stdin, heredoc, --body-file -) are refused (#470) and name --body-file
    verdict, reason = run("gh issue comment 1 --body-file -", role="tester")
    check("--body-file - is refused", verdict, "refuse")
    check("--body-file - refusal names --body-file", "--body-file" in reason, True)

    verdict, reason = run("echo 'hi' | gh issue comment 1", role="tester")
    check("stdin pipe comment is refused", verdict, "refuse")
    check("stdin pipe refusal names --body-file", "--body-file" in reason, True)

    verdict, reason = run("gh issue comment 1 <<EOF\n<!-- tester -->\nbody\nEOF", role="tester")
    check("heredoc comment is refused", verdict, "refuse")
    check("heredoc refusal names --body-file", "--body-file" in reason, True)

    # …but the dominant shape in this repo writes the heredoc to a file and then
    # names that file, in one Bash call. The body is on disk and readable, and a
    # bare `<<` test in cli_comment_status would refuse every session in the loop.
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write("<!-- monitor -->\nbody\n")
        path = fh.name
    verdict, _ = run(
        f"cat > {path} <<'MD'\n<!-- monitor -->\nbody\nMD\ngh issue comment 1 --body-file {path}",
        role="monitor",
    )
    check("heredoc written to a file, then --body-file, is allowed", verdict, "allow")
    pathlib.Path(path).unlink()


def test_the_aide_files_and_comments_but_does_not_move_the_queue() -> None:
    """#470: the owner's ad-hoc sessions get a role rather than the fail-open gap."""
    verdict, _ = run("gh issue create --title t --body '<!-- aide --> found this'", role="aide")
    check("a signed aide filing is allowed", verdict, "allow")

    verdict, _ = run("gh issue create --title t --body 'found this'", role="aide")
    check("an unsigned aide filing is refused", verdict, "refuse")

    verdict, _ = run("gh issue comment 1 --body '<!-- monitor --> hi'", role="aide")
    check("the aide may not sign as the monitor", verdict, "refuse")

    # It says how bad the thing looks, and never re-ranks anyone else's issue.
    verdict, _ = run("gh issue edit 1 --add-label P1", role="aide")
    check("the aide may set a priority", verdict, "allow")

    verdict, _ = run("gh issue edit 1 --remove-label P2", role="aide")
    check("the aide may not clear a priority", verdict, "refuse")

    # Nothing that moves the queue.
    verdict, _ = run("gh issue close 1 --reason 'not planned'", role="aide")
    check("the aide may not close", verdict, "refuse")

    verdict, _ = run("gh issue edit 1 --add-label stage:ready", role="aide")
    check("the aide may not apply stage:ready", verdict, "refuse")

    verdict, _ = run("gh issue edit 1 --add-label stage:verified", role="aide")
    check("the aide may not apply stage:verified", verdict, "refuse")

    verdict, _ = run("gh pr merge 7 --squash", role="aide")
    check("the aide may not merge", verdict, "refuse")

    # stage:triage is listed in LABEL_RULES, allowing aide to file and clear on manual triage.
    verdict, _ = run("gh issue edit 1 --add-label stage:triage", role="aide")
    check("the aide may file at stage:triage", verdict, "allow")

    verdict, _ = run("gh issue edit 1 --remove-label stage:triage", role="aide")
    check("the aide may remove stage:triage", verdict, "allow")


def test_rest_comments_need_the_marker() -> None:
    # #314: gh api repos/o/r/issues/296/comments -f body=...
    for role in ("tester", "monitor", "reviewer", "coder"):
        verdict, _ = run("gh api repos/o/r/issues/296/comments -f body='no marker'", role=role)
        check(f"unsigned REST comment is refused for {role}", verdict, "refuse")

    verdict, _ = run("gh api repos/o/r/issues/296/comments -f body='<!-- tester --> valid'", role="tester")
    check("signed REST comment passes for tester", verdict, "allow")

    verdict, _ = run("gh api repos/o/r/issues/296/comments -f body='<!-- tester --> valid'", role="monitor")
    check("REST comment signed with another role is refused", verdict, "refuse")

    # Flag variations: --field, --raw-field, -F
    verdict, _ = run("gh api repos/o/r/issues/296/comments --field body='no marker'", role="tester")
    check("--field unsigned is refused", verdict, "refuse")

    verdict, _ = run("gh api repos/o/r/issues/296/comments --raw-field body='<!-- tester --> valid'", role="tester")
    check("--raw-field signed passes", verdict, "allow")

    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write("<!-- tester -->\nbody from file\n")
        path = fh.name
    verdict, _ = run(f"gh api repos/o/r/issues/296/comments -F body=@{path}", role="tester")
    check("-F body=@path signed passes", verdict, "allow")
    pathlib.Path(path).unlink()

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump({"body": "<!-- tester -->\njson body\n"}, fh)
        path = fh.name
    verdict, _ = run(f"gh api repos/o/r/issues/296/comments --input {path}", role="tester")
    check("--input path signed passes", verdict, "allow")
    pathlib.Path(path).unlink()

    # Unreadable forms are refused (#470) with a message naming --body-file
    verdict, reason = run("gh api repos/o/r/issues/296/comments --input -", role="tester")
    check("--input - is refused", verdict, "refuse")
    check("--input - refusal names --body-file", "--body-file" in reason, True)

    verdict, reason = run("gh api repos/o/r/issues/296/comments -F body=@-", role="tester")
    check("-F body=@- is refused", verdict, "refuse")
    check("-F body=@- refusal names --body-file", "--body-file" in reason, True)

    # Read requests are allowed without marker
    verdict, _ = run("gh api repos/o/r/issues/296/comments", role="tester")
    check("GET comments needs no marker", verdict, "allow")


def test_label_rules() -> None:
    verdict, _ = run("gh issue edit 1 --add-label stage:ready", role="monitor")
    check("the monitor may not apply stage:ready", verdict, "refuse")

    verdict, _ = run("gh issue edit 1 --add-label stage:ready", role="reviewer")
    check("the reviewer may", verdict, "allow")

    verdict, _ = run("gh issue edit 1 --add-label 'on hold'", role="reviewer")
    check("nobody may apply on hold", verdict, "refuse")

    verdict, _ = run("gh issue edit 1 --add-label P0", role="coder")
    check("the coder may not re-prioritise", verdict, "refuse")

    verdict, _ = run("gh issue edit 1 --remove-label P1 --add-label P0", role="monitor")
    check("the monitor may", verdict, "allow")

    verdict, _ = run("gh issue edit 1 --remove-label stage:ready", role="monitor")
    check("the monitor may remove stage:ready", verdict, "allow")

    verdict, _ = run("gh issue edit 1 --remove-label stage:pr", role="monitor")
    check("the monitor may remove stage:pr", verdict, "allow")


def test_stage_transitions() -> None:
    # Multiple stage labels cannot be added at once
    verdict, _ = run("gh issue edit 1 --add-label stage:ready,stage:pr", role="reviewer")
    check("multiple stage labels at once is refused", verdict, "refuse")

    verdict, _ = run("gh issue edit 1 --add-label stage:triage --add-label stage:ready", role="reviewer")
    check("multiple stage flags at once is refused", verdict, "refuse")

    # Advancing to stage:pr must clear prior stage labels (stage:triage, stage:ready, stage:owner) (#333)
    verdict, _ = run("gh issue edit 1 --add-label stage:pr", role="reviewer")
    check("adding stage:pr with no remove is refused", verdict, "refuse")

    # The triage -> pr shortcut that left stage:triage attached (#333)
    verdict, reason = run("gh issue edit 1 --add-label stage:pr --remove-label stage:ready", role="reviewer")
    check("adding stage:pr while only removing stage:ready is refused", verdict, "refuse")
    check("refusal mentions missing stage:triage", "stage:triage" in reason, True)

    verdict, _ = run("gh issue edit 1 --add-label stage:pr --remove-label stage:triage", role="reviewer")
    check("adding stage:pr while only removing stage:triage is refused", verdict, "refuse")

    verdict, reason = run("gh issue edit 1 --add-label stage:pr --remove-label stage:ready,stage:triage", role="reviewer")
    check("adding stage:pr without removing stage:owner is refused", verdict, "refuse")
    check("refusal mentions missing stage:owner", "stage:owner" in reason, True)

    # Fully clearing prior stages is allowed for reviewer
    verdict, _ = run(
        "gh issue edit 1 --add-label stage:pr --remove-label stage:ready,stage:triage,stage:owner",
        role="reviewer",
    )
    check("adding stage:pr while removing all prior stages is allowed for reviewer", verdict, "allow")

    verdict, _ = run(
        "gh issue edit 1 --add-label stage:pr --remove-label stage:triage,stage:ready,stage:owner",
        role="reviewer",
    )
    check("order of remove labels does not matter", verdict, "allow")

    verdict, _ = run(
        "gh issue edit 1 --add-label stage:pr --remove-label stage:ready --remove-label stage:triage --remove-label stage:owner",
        role="reviewer",
    )
    check("multiple --remove-label flags clearing prior stages is allowed", verdict, "allow")

    # Coder or monitor cannot add stage:pr even with proper removes
    verdict, _ = run(
        "gh issue edit 1 --add-label stage:pr --remove-label stage:ready,stage:triage,stage:owner",
        role="coder",
    )
    check("coder cannot add stage:pr", verdict, "refuse")


def test_host_shapes() -> None:
    """The first defect: what "allowed" looks like on the wire, per host.

    Claude Code reads a `decision`-less object as "no opinion, proceed". The
    Antigravity CLI reads the same object as a refusal, with no reason attached —
    which is how the coder came to be blocked on `echo "$CC_ROLE"` with a blank
    message on 2026-09-11.
    """
    payload = {"tool_input": {"command": "git status"}, "session_id": "s"}
    out = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, "CC_ROLE": "monitor"},
        check=True,
    ).stdout
    check("Claude Code gets a bare {} for an allow", json.loads(out), {})

    payload = {
        "toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}},
        "conversationId": "c",
    }
    out = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, "CC_ROLE": "coder"},
        check=True,
    ).stdout
    check("agy gets an explicit allow", json.loads(out).get("decision"), "allow")

    verdict, _ = run("gh issue edit 1 --add-label 'on hold'", role="coder", host=AGY)
    check("and an explicit deny", verdict, "refuse")


# --------------------------------------------------------------------------- #
# The fail-open that failed closed (#268)
#
# `__main__` catches everything main() throws and lets the command run, because a
# guard that bricks the loop over an unusual payload costs more than the gap it
# closes. That only works if the handler answers in the host's own vocabulary,
# and it could not: the host was read out of the payload, so a payload that would
# not parse left it at Claude Code's default. On agy the handler then printed no
# `decision` at all, which agy reads as a refusal — the fail-open inverted into a
# fail-closed on exactly the input it exists to survive.
# --------------------------------------------------------------------------- #


def raw_full(payload: str, role: str = "coder") -> tuple[dict, str]:
    """Drive the guard with bytes rather than a dict, so the parse can fail.

    Returns stderr as well, because on agy that is where the guard has to put
    anything it wants to say — see #277 and the case below that pins it.
    """
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "CC_ROLE": role},
        check=True,
    )
    return json.loads(proc.stdout or "{}"), proc.stderr


def raw(payload: str, role: str = "coder") -> dict:
    return raw_full(payload, role)[0]


def test_unparsable_payload_fails_open_on_both_hosts() -> None:
    # Truncated mid-object: valid enough to say who is asking, not to parse.
    agy = '{"conversationId": "c", "toolCall": {"args": {"CommandLine": "git st'
    out, err = raw_full(agy)
    check("agy: a broken payload still gets an explicit allow",
          out.get("decision"), "allow")
    check("agy: and is told why, on stderr", "role_guard error" in err, True)

    cc = '{"session_id": "s", "tool_input": {"command": "git st'
    out = raw(cc)
    check("Claude Code: a broken payload carries no decision",
          "decision" in out, False)
    check("Claude Code: and is told why",
          "role_guard error" in out.get("systemMessage", ""), True)


def test_the_agy_error_object_declares_only_what_agy_declares() -> None:
    """#277. `PreToolHookResult` has decision, reason, overwrite,
    permission_overrides, allow_tool and deny_reason — and no `systemMessage`.
    The binary carries `DisallowUnknownFields`, so writing a field agy has no
    contract for risks the object being rejected *inside the error handler*,
    which is the one place a fail-open must not fail."""
    out, err = raw_full('{"conversationId": "c", "toolCall": {"args": {"Comm')
    allowed = {"decision", "reason", "overwrite", "permission_overrides",
               "permissionOverrides", "allow_tool", "deny_reason"}
    check("agy: the error object declares no field agy does not have",
          sorted(set(out) - allowed), [])
    check("agy: the note is not lost, it is on stderr",
          "role_guard error" in err, True)


def test_an_unknown_envelope_from_recognized_host_is_refused() -> None:
    """#643. When the host is recognised (matches AGY_PAYLOAD or CC_PAYLOAD)
    but the envelope is unknown or unparseable, allowing it is a fail-open defect
    that lets commands run completely unguarded (#277 allowed this, leading to
    unguarded merges under agy). The guard must fail closed with a refusal."""
    out = raw('{"conversationId": "c", "someNewCall": {"args": {"CommandLine": "ls"}}}')
    check("agy: an unknown envelope from a recognized host is refused",
          out.get("decision"), "deny")

    # The same shape on Claude Code must also fail closed with block.
    out = raw('{"session_id": "s", "some_new_input": {"command": "ls"}}')
    check("Claude Code: an unknown envelope from a recognized host is refused",
          out.get("decision"), "block")


def test_host_is_read_before_the_parse() -> None:
    """Not JSON at all, so nothing downstream of json.loads can classify it."""
    out = raw("this is not json, and never was")
    check("garbage from an unidentifiable host falls back to Claude Code's shape",
          "decision" in out, False)

    # The tie-break is deliberate rather than incidental: Claude Code's shape
    # means "no opinion, ask the owner as usual", agy's explicit allow skips the
    # prompt. An unrecognised payload gets the one that still asks.
    out = raw('{"session_id": "s", "conversationId": "c", "tool_input": {"command')
    check("a payload claiming both hosts is treated as Claude Code",
          "decision" in out, False)


def test_the_word_toolcall_inside_a_command_is_not_the_host() -> None:
    """A Claude Code payload whose *command* mentions agy's keys is still Claude
    Code. JSON escapes the quotes around a string inside a string, which is what
    makes the quoted-key match safe — and this is not hypothetical, since the
    command that finds these keys is a grep for them."""
    out = raw(json.dumps({
        "session_id": "s",
        "tool_input": {"command": 'grep -rn \'"toolCall":\' .claude/hooks/'},
    }))
    check("the grep is allowed", out, {})

    # Now the same command in a payload truncated past the point of parsing.
    body = json.dumps({"tool_input": {"command": 'grep -rn \'"toolCall":\' x'}})
    out = raw(body[:-8])
    check("and a truncated one does not become an agy auto-approve",
          "decision" in out, False)


def test_unparseable_command_payload_from_recognized_host_is_refused() -> None:
    """Issue #643: A recognized host (AGY or CC) sending a command hook payload that
    cannot be parsed into a command must be refused (fail-closed), not allowed."""
    out_agy = raw('{"conversationId": "c", "toolCall": {"name": "run_command", "args": {}}}')
    check("agy: unparseable command payload is refused with deny",
          out_agy.get("decision"), "deny")

    out_cc = raw('{"session_id": "s", "tool_input": {}}')
    check("Claude Code: unparseable command payload is refused with block",
          out_cc.get("decision"), "block")


def test_heartbeat_and_check_alive() -> None:
    """Issues #643, #656: Prove heartbeat recording, per-session tracking, test isolation and --check-alive."""
    import time
    from role_guard import HEARTBEAT_FILE, RUNS_DIR

    # 1. Heartbeat and session file creation in isolated test environment
    check("heartbeat file uses isolated directory", str(TEST_HEARTBEAT), str(HEARTBEAT_FILE))
    check("runs dir uses isolated directory", str(TEST_RUNS), str(RUNS_DIR))

    sess = "test-heartbeat-sess"
    out = raw('{"session_id": "%s", "tool_input": {"command": "git status"}}' % sess, role="coder")
    check("role_guard runs cleanly", "decision" in out, False)
    check("heartbeat file exists in test dir", HEARTBEAT_FILE.is_file(), True)
    sess_file = RUNS_DIR / sess
    check("session record exists", sess_file.is_file(), True)
    if sess_file.is_file():
        content = sess_file.read_text(encoding="utf-8")
        check("session record contains role", "coder" in content, True)
        check("session record contains command", "git status" in content, True)

    # 2. --check-alive with valid active session ID returns 0
    proc = subprocess.run(
        [sys.executable, str(GUARD), "--check-alive", sess],
        capture_output=True,
        text=True,
        env=os.environ,
    )
    check("--check-alive <valid_session> returns 0", proc.returncode, 0)
    check("--check-alive <valid_session> reports active", f"role_guard is active for session {sess}" in proc.stdout, True)

    # 3. --check-alive with nonexistent session ID returns 1 (#656)
    proc_nonexistent = subprocess.run(
        [sys.executable, str(GUARD), "--check-alive", "nonexistent-session-12345"],
        capture_output=True,
        text=True,
        env=os.environ,
    )
    check("--check-alive <nonexistent_session> returns 1", proc_nonexistent.returncode, 1)
    check("--check-alive <nonexistent_session> reports not found", "heartbeat not found for session nonexistent-session-12345" in proc_nonexistent.stderr, True)

    # 4. --check-alive with stale session returns 1 (#656)
    stale_sess = "stale-session-test"
    stale_file = RUNS_DIR / stale_sess
    stale_file.write_text("old\n", encoding="utf-8")
    past_time = time.time() - 3600
    os.utime(stale_file, (past_time, past_time))
    proc_stale = subprocess.run(
        [sys.executable, str(GUARD), "--check-alive", stale_sess],
        capture_output=True,
        text=True,
        env=os.environ,
    )
    check("--check-alive <stale_session> returns 1", proc_stale.returncode, 1)
    check("--check-alive <stale_session> reports stale", "is stale" in proc_stale.stderr, True)

    # 5. --check-alive without session ID when active session exists returns 0
    proc_noarg = subprocess.run(
        [sys.executable, str(GUARD), "--check-alive"],
        capture_output=True,
        text=True,
        env=os.environ,
    )
    check("--check-alive (no args) with active sessions returns 0", proc_noarg.returncode, 0)
    check("--check-alive (no args) reports active", "role_guard is active" in proc_noarg.stdout, True)

    # 6. --check-alive without session ID when RUNS_DIR is empty returns 1 (#656)
    with tempfile.TemporaryDirectory(prefix="empty_runs_") as empty_dir:
        env_empty = {**os.environ, "ROLE_GUARD_RUNS_DIR": empty_dir}
        proc_empty = subprocess.run(
            [sys.executable, str(GUARD), "--check-alive"],
            capture_output=True,
            text=True,
            env=env_empty,
        )
        check("--check-alive empty runs dir returns 1", proc_empty.returncode, 1)
        check("--check-alive empty runs dir reports empty", "runs directory is empty" in proc_empty.stderr, True)

    # 7. --check-alive without session ID when all sessions are stale returns 1 (#656)
    with tempfile.TemporaryDirectory(prefix="stale_runs_") as stale_dir:
        stale_p = pathlib.Path(stale_dir) / "old-session"
        stale_p.write_text("old\n", encoding="utf-8")
        os.utime(stale_p, (past_time, past_time))
        env_stale = {**os.environ, "ROLE_GUARD_RUNS_DIR": stale_dir}
        proc_all_stale = subprocess.run(
            [sys.executable, str(GUARD), "--check-alive"],
            capture_output=True,
            text=True,
            env=env_stale,
        )
        check("--check-alive all stale returns 1", proc_all_stale.returncode, 1)
        check("--check-alive all stale reports stale across all", "stale across all" in proc_all_stale.stderr, True)

    # Clean up stale session from TEST_RUNS so subsequent tests don't see it
    try:
        stale_file.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# `hold:monitor` (#490)
#
# On 2026-09-12 the monitor commented "Hold this patch" on #476 at 21:41Z, PR #484
# was opened at 21:45Z containing the function the hold named, and it merged at
# 21:53Z. The hold was prose and the coder polls labels, so nothing had to go
# wrong for it to be ignored. The owner's answer, 2026-09-13, was a label the
# guard enforces.
#
# These cases stub `gh` on PATH rather than the network: the guard shells out to
# `gh issue view --json labels`, and a test that reached GitHub would be slow,
# flaky, and would need a real held issue to exist.
# --------------------------------------------------------------------------- #

FAKE_GH = '''#!/usr/bin/env python3
import json, os, pathlib, sys

args = sys.argv[1:]
state = json.loads(pathlib.Path(os.environ["FAKE_GH_STATE"]).read_text())
pathlib.Path(os.environ["FAKE_GH_CALLED"]).write_text("yes")

if state.get("broken"):
    sys.exit(1)
if args[:2] == ["issue", "view"]:
    names = state.get("labels", {}).get(args[2], [])
    bodies = state.get("comments", {}).get(args[2], [])
    print(json.dumps({
        "labels": [{"name": n} for n in names],
        "comments": [{"body": b} for b in bodies],
    }))
elif args[:2] == ["pr", "view"]:
    key = args[2] if len(args) > 2 and args[2].isdigit() else ""
    refs = state.get("linked", {}).get(key, [])
    pr_state = state.get("prs", {}).get(key)
    if pr_state is not None:
        head = pr_state.get("headRefOid", "0123456789abcdef0123456789abcdef01234567")
        body = pr_state.get("body", f"<!-- reviewer -->\\nCloses #{key}\\n- **Sweep SHA:** {head}\\n- **Sweep:** waived - owner ruling\\n- **Fail-before:** waived - owner ruling\\n")
        files = pr_state.get("files", [])
    else:
        head = "0123456789abcdef0123456789abcdef01234567"
        body = f"<!-- reviewer -->\\nCloses #{key}\\n- **Sweep SHA:** {head}\\n- **Sweep:** waived - owner ruling\\n- **Fail-before:** waived - owner ruling\\n"
        files = []
    print(json.dumps({
        "closingIssuesReferences": [{"number": int(n)} for n in refs],
        "headRefOid": head,
        "baseRefOid": (pr_state or {}).get("baseRefOid", ""),
        "body": body,
        "files": files,
    }))
else:
    sys.exit(1)
'''


def make_clean_repo() -> pathlib.Path:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="role-guard-repo-"))
    env = dict(os.environ)
    subprocess.check_call(["git", "init", "-b", "main", str(tmp)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.check_call(["git", "-C", str(tmp), "config", "user.name", "Tester"], env=env)
    subprocess.check_call(["git", "-C", str(tmp), "config", "user.email", "test@example.com"], env=env)
    subprocess.check_call(["git", "-C", str(tmp), "commit", "--allow-empty", "-m", "init"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    head = subprocess.check_output(["git", "-C", str(tmp), "rev-parse", "HEAD"], env=env).decode().strip()
    subprocess.check_call(["git", "-C", str(tmp), "update-ref", "refs/remotes/origin/main", head], env=env)
    return tmp


def with_fake_gh(
    labels: dict | None = None,
    linked: dict | None = None,
    broken: bool = False,
    prs: dict | None = None,
    comments: dict | None = None,
):
    """A PATH with a stub `gh` on it, plus the path of its call marker."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="role-guard-gh-"))
    (tmp / "gh").write_text(FAKE_GH)
    (tmp / "gh").chmod(0o755)
    state = tmp / "state.json"
    state.write_text(json.dumps(
        {
            "labels": labels or {},
            "linked": linked or {},
            "broken": broken,
            "prs": prs or {},
            "comments": comments or {},
        }
    ))
    called = tmp / "called"
    repo = make_clean_repo()
    return {
        "PATH": f"{tmp}:{os.environ.get('PATH', '')}",
        "FAKE_GH_STATE": str(state),
        "FAKE_GH_CALLED": str(called),
        "SHARED_REPO_PATH": str(repo),
    }, called


def make_commit(files: dict[str, str], parent: str = "") -> str:
    """Create a git commit object with the specified files and return its SHA.

    `parent` gives the commit an ancestor, which the case-count gate needs: it
    compares against `git merge-base`, and two parentless commits have none.
    """
    env = dict(os.environ)
    fd, path = tempfile.mkstemp(prefix="git-index-")
    os.close(fd)
    os.unlink(path)
    try:
        env["GIT_INDEX_FILE"] = path
        subprocess.check_call(["git", "read-tree", "--empty"], env=env)
        for p, content in files.items():
            blob = subprocess.check_output(
                ["git", "hash-object", "-w", "--stdin"],
                input=content.encode(),
                env=env,
            ).decode().strip()
            subprocess.check_call(
                ["git", "update-index", "--add", "--cacheinfo", "100644", blob, p],
                env=env,
            )
        tree = subprocess.check_output(["git", "write-tree"], env=env).decode().strip()
        # A unique message, because commit identity is (tree, parent, message,
        # author, second). Two calls with the same tree and parent inside one
        # second collide, and the fork-point case then measures a commit against
        # itself and reports no growth - a flake, roughly one run in three.
        argv = ["git", "commit-tree", tree, "-m", f"test commit {uuid.uuid4()}"]
        if parent:
            argv += ["-p", parent]
        commit = subprocess.check_output(argv, env=env).decode().strip()
        return commit
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_hold_monitor_stops_the_close_and_the_merge() -> None:
    env, _ = with_fake_gh(labels={"476": ["P0", "hold:monitor"], "477": ["P2"]},
                          linked={"484": ["476"], "483": ["477"]})

    verdict, reason = run("gh issue close 476 --reason completed",
                          role="reviewer", extra_env=env)
    check("a held issue does not close", verdict, "refuse")
    check("and the refusal names the issue", "#476" in reason, True)

    verdict, _ = run("gh pr merge 484 --squash", role="coder", extra_env=env)
    check("a PR whose linked issue is held does not merge", verdict, "refuse")

    verdict, _ = run("gh issue close 477", role="reviewer", extra_env=env)
    check("an unheld issue still closes", verdict, "allow")

    verdict, _ = run("gh pr merge 483 --squash", role="coder", extra_env=env)
    check("and its PR still merges", verdict, "allow")

    # The REST spellings of both, which the label rules already had to learn once
    # (#289) and which would otherwise be the way around this in a fortnight.
    verdict, _ = run(
        "gh api -X PATCH repos/o/r/issues/476 -f state=closed",
        role="reviewer", extra_env=env)
    check("the REST close is held too", verdict, "refuse")

    verdict, _ = run("gh api -X PUT repos/o/r/pulls/484/merge",
                     role="coder", extra_env=env)
    check("the REST merge is held too", verdict, "refuse")


def test_lifting_the_hold_in_the_same_command_is_the_way_through() -> None:
    env, _ = with_fake_gh(labels={"476": ["hold:monitor"]})
    verdict, _ = run(
        "gh issue edit 476 --remove-label hold:monitor && gh issue close 476",
        role="monitor", extra_env=env)
    check("removing the hold and closing in one command is allowed",
          verdict, "allow")

    # Nobody else can do that, because nobody else can clear the label.
    verdict, _ = run(
        "gh issue edit 476 --remove-label hold:monitor && gh issue close 476",
        role="coder", extra_env=env)
    check("the coder cannot lift it on its way past", verdict, "refuse")


def test_only_the_monitor_sets_the_hold() -> None:
    for role in ("coder", "reviewer", "tester", "aide"):
        verdict, _ = run(f"gh issue edit 476 --add-label {'hold:monitor'}", role=role)
        check(f"the {role} cannot apply hold:monitor", verdict, "refuse")
        verdict, _ = run("gh issue edit 476 --remove-label hold:monitor", role=role)
        check(f"the {role} cannot clear hold:monitor", verdict, "refuse")

    verdict, _ = run("gh issue edit 476 --add-label hold:monitor", role="monitor")
    check("the monitor applies it", verdict, "allow")
    verdict, _ = run("gh issue edit 476 --remove-label hold:monitor", role="monitor")
    check("and clears it", verdict, "allow")


def test_the_hold_lookup_fails_open_and_is_not_run_on_other_writes() -> None:
    """Two properties, and both are load-bearing.

    Fail-open, because this is the guard's only network call and a loop that
    stops when GitHub is slow costs more than the rule saves. And *not run*
    on anything but a close or a merge, because every Bash call goes through
    here — a query on each one would be felt immediately.
    """
    env, called = with_fake_gh(labels={"476": ["hold:monitor"]}, broken=True)
    verdict, _ = run("gh issue close 476", role="reviewer", extra_env=env)
    check("a gh that cannot answer allows the close", verdict, "allow")

    env, called = with_fake_gh(labels={"476": ["hold:monitor"]})
    run("gh issue comment 476 --body '<!-- monitor --> hi'",
        role="monitor", extra_env=env)
    check("commenting on a held issue queries nothing", called.exists(), False)
    check("and is allowed", run("gh issue comment 476 --body '<!-- monitor --> hi'",
                                role="monitor", extra_env=env)[0], "allow")


def test_pr_merge_requires_sweep_sha() -> None:
    env, _ = with_fake_gh(
        prs={"100": {
            "headRefOid": "1111111222222233333333444444445555555566",
            "body": "<!-- reviewer -->\nCloses #100\nNo sweep SHA here\n",
            "files": [],
        }}
    )
    verdict, reason = run("gh pr merge 100 --squash", role="coder", extra_env=env)
    check("PR without sweep SHA is refused", verdict, "refuse")
    check("refusal mentions missing Sweep SHA", "missing `Sweep SHA`" in reason, True)


def test_pr_merge_rejects_stale_sweep_sha() -> None:
    env, _ = with_fake_gh(
        prs={"101": {
            "headRefOid": "2222222333333344444444555555556666666677",
            "body": "<!-- reviewer -->\nCloses #101\n- **Sweep SHA:** 1111111\n",
            "files": [],
        }}
    )
    verdict, reason = run("gh pr merge 101 --squash", role="coder", extra_env=env)
    check("PR with stale sweep SHA is refused", verdict, "refuse")
    check("refusal mentions does not match", "does not match Sweep SHA" in reason, True)

    verdict, reason = run(
        "gh api -X PUT repos/o/r/pulls/101/merge",
        role="coder",
        extra_env=env,
    )
    check("REST merge with stale sweep SHA is refused", verdict, "refuse")


def test_pr_merge_allows_matching_sweep_sha() -> None:
    commit = make_commit({"clean.py": "def foo() -> int:\n    return 42\n"})
    env, _ = with_fake_gh(
        prs={"102": {
            "headRefOid": commit,
            "body": (f"<!-- reviewer -->\nCloses #102\n- **Sweep SHA:** {commit[:7]}\n"
                     "- **Fail-before:** tools/clean_check.py 41 -> 42\n"),
            "files": [
                {"path": "clean.py", "changeType": "MODIFIED"},
                {"path": "tools/clean_check.py", "changeType": "MODIFIED", "additions": 6},
            ],
        }}
    )
    verdict, _ = run("gh pr merge 102 --squash", role="coder", extra_env=env)
    check("PR with matching sweep SHA and valid python is allowed", verdict, "allow")


def test_pr_merge_detects_conflict_markers() -> None:
    for marker in ("<<<<<<< HEAD", "=======", ">>>>>>> issue-560"):
        commit = make_commit({
            "tools/deploy_check.py": f"# header\n{marker}\ndef tester_cases():\n    pass\n"
        })
        env, _ = with_fake_gh(
            prs={"103": {
                "headRefOid": commit,
                "body": f"<!-- reviewer -->\nCloses #103\n- **Sweep SHA:** {commit}\n",
                "files": [{"path": "tools/deploy_check.py", "changeType": "MODIFIED"}],
            }}
        )
        verdict, reason = run("gh pr merge 103 --squash", role="coder", extra_env=env)
        check(f"PR with conflict marker {marker[:7]} is refused", verdict, "refuse")
        check("refusal mentions conflict marker", "Conflict marker" in reason, True)


def test_pr_merge_detects_syntax_errors() -> None:
    commit = make_commit({
        "tools/deploy_check.py": "def invalid_syntax(\n"
    })
    env, _ = with_fake_gh(
        prs={"104": {
            "headRefOid": commit,
            "body": f"<!-- reviewer -->\nCloses #104\n- **Sweep SHA:** {commit}\n",
            "files": [{"path": "tools/deploy_check.py", "changeType": "MODIFIED"}],
        }}
    )
    verdict, reason = run("gh pr merge 104 --squash", role="coder", extra_env=env)
    check("PR with syntax error is refused", verdict, "refuse")
    check("refusal mentions syntax error", "Syntax error" in reason, True)


def test_pr_merge_refuses_when_shared_checkout_dirty() -> None:
    for scoped in ("src", "tools", ".claude", ".agents"):
        repo = make_clean_repo()
        target_dir = repo / scoped
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "dirty_file.txt").write_text("unreviewed write\n")
        commit = make_commit({"clean.py": "def foo() -> int:\n    return 42\n"})
        env, _ = with_fake_gh(
            prs={"201": {
                "headRefOid": commit,
                "body": (f"<!-- reviewer -->\nCloses #201\n- **Sweep SHA:** {commit[:7]}\n"
                         "- **Fail-before:** waived - docs only\n"),
                "files": [{"path": "clean.py", "changeType": "MODIFIED"}],
            }}
        )
        env["SHARED_REPO_PATH"] = str(repo)
        verdict, reason = run("gh pr merge 201 --squash", role="coder", extra_env=env)
        check(f"PR merge when {scoped} is dirty is refused", verdict, "refuse")
        check(f"refusal for dirty {scoped} mentions dirty checkout", "is dirty" in reason, True)
        check(f"refusal for dirty {scoped} mentions unreviewed writes", "Unreviewed writes" in reason, True)


def test_pr_merge_allows_when_untracked_files_outside_app_and_loop() -> None:
    repo = make_clean_repo()
    unrelated = repo / "2026-0914-web2pdf"
    unrelated.mkdir(parents=True, exist_ok=True)
    (unrelated / "notes.txt").write_text("scratch in another project\n")
    (repo / "scratch.md").write_text("random note\n")
    commit = make_commit({"clean.py": "def foo() -> int:\n    return 42\n"})
    env, _ = with_fake_gh(
        prs={"204": {
            "headRefOid": commit,
            "body": (f"<!-- reviewer -->\nCloses #204\n- **Sweep SHA:** {commit[:7]}\n"
                     "- **Fail-before:** waived - docs only\n"),
            "files": [{"path": "clean.py", "changeType": "MODIFIED"}],
        }}
    )
    env["SHARED_REPO_PATH"] = str(repo)
    verdict, _ = run("gh pr merge 204 --squash", role="coder", extra_env=env)
    check("PR merge with any untracked project file is refused", verdict, "refuse")


def test_pr_merge_refuses_when_local_main_ahead_of_origin_main() -> None:
    repo = make_clean_repo()
    (repo / "stray.txt").write_text("stray write\n")
    git_env = dict(os.environ)
    subprocess.check_call(["git", "-C", str(repo), "add", "stray.txt"], env=git_env)
    subprocess.check_call(["git", "-C", str(repo), "commit", "-m", "stray commit directly on main"], env=git_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    commit = make_commit({"clean.py": "def foo() -> int:\n    return 42\n"})
    env, _ = with_fake_gh(
        prs={"202": {
            "headRefOid": commit,
            "body": f"<!-- reviewer -->\nCloses #202\n- **Sweep SHA:** {commit[:7]}\n",
            "files": [{"path": "clean.py", "changeType": "MODIFIED"}],
        }}
    )
    env["SHARED_REPO_PATH"] = str(repo)
    verdict, reason = run("gh pr merge 202 --squash", role="coder", extra_env=env)
    check("PR merge when local main has stray commits is refused", verdict, "refuse")
    check("refusal mentions stray commits", "stray commits" in reason, True)
    check("refusal mentions ahead of origin/main", "ahead of `origin/main`" in reason, True)


def test_pr_merge_allows_when_shared_checkout_clean() -> None:
    repo = make_clean_repo()
    commit = make_commit({"clean.py": "def foo() -> int:\n    return 42\n"})
    env, _ = with_fake_gh(
        prs={"203": {
            "headRefOid": commit,
            "body": (f"<!-- reviewer -->\nCloses #203\n- **Sweep SHA:** {commit[:7]}\n"
                     "- **Fail-before:** waived - docs only\n"),
            "files": [{"path": "clean.py", "changeType": "MODIFIED"}],
        }}
    )
    env["SHARED_REPO_PATH"] = str(repo)
    verdict, _ = run("gh pr merge 203 --squash", role="coder", extra_env=env)
    check("PR merge when shared checkout is clean is allowed", verdict, "allow")


def test_pr_merge_requires_fail_before() -> None:
    """Gate 3, owner's ruling 2026-09-14. The #632 shape is the third case here:
    counts equal, expected value rewritten, 22/22 green, reopened in 13 minutes."""
    commit = make_commit({"clean.py": "def foo() -> int:\n    return 42\n"})
    base = f"<!-- reviewer -->\nCloses #{{n}}\n- **Sweep SHA:** {commit[:7]}\n"
    code_only = [{"path": "clean.py", "changeType": "MODIFIED", "additions": 4}]
    with_case = code_only + [
        {"path": "tools/clean_check.py", "changeType": "MODIFIED", "additions": 6}
    ]

    def verdict_for(body: str, files: list, key: str = "301") -> tuple[str, str]:
        env, _ = with_fake_gh(
            prs={key: {"headRefOid": commit, "body": body, "files": files}}
        )
        return run(f"gh pr merge {key} --squash", role="coder", extra_env=env)

    verdict, reason = verdict_for(base.format(n=301), with_case)
    check("PR with no Fail-before line is refused", verdict, "refuse")
    check("refusal names Fail-before", "missing `Fail-before`" in reason, True)

    verdict, reason = verdict_for(
        base.format(n=302) + "- **Fail-before:** tools/clean_check.py 368 -> 368\n",
        with_case, "302")
    check("equal counts are refused (#632)", verdict, "refuse")
    check("refusal says the fix is pinned by nothing",
          "pinned by nothing" in reason, True)

    verdict, reason = verdict_for(
        base.format(n=303) + "- **Fail-before:** tools/clean_check.py 367 -> 368\n",
        code_only, "303")
    check("a rising count with no added check line is refused", verdict, "refuse")
    check("refusal names the rewritten expectation",
          "expected value was rewritten" in reason, True)

    verdict, _ = verdict_for(
        base.format(n=304) + "- **Fail-before:** tools/clean_check.py 367 -> 368\n",
        with_case, "304")
    check("a real fail-before with an added case is allowed", verdict, "allow")

    for reason_text in ("needs: tester repro", "loop/process", "docs only",
                        "owner ruling"):
        verdict, _ = verdict_for(
            base.format(n=305) + f"- **Fail-before:** waived - {reason_text}\n",
            code_only, "305")
        check(f"waiver '{reason_text}' is allowed", verdict, "allow")

    verdict, _ = verdict_for(
        base.format(n=306) + "- **Fail-before:** waived - I could not be bothered\n",
        code_only, "306")
    check("an unrecognised waiver reason is refused", verdict, "refuse")


def test_pr_merge_counts_cases_not_lines() -> None:
    """The #632 shape: lines change in the check script, cases do not, because an
    existing expected value was inverted to match the new behaviour."""
    src = "def where() -> str:\n    return 'x'\n"
    two_cases = (
        "def screensaver_cases():\n"
        "    check('a', where(), 'Hakone')\n"
        "    check('b', where(), 'Odawara')\n"
    )
    rewritten = (
        "def screensaver_cases():\n"
        "    check('a renamed, and its expectation inverted', where(), 'x')\n"
        "    check('b', where(), 'Odawara')\n"
    )
    three_cases = two_cases + "    check('c', where(), 'Atami')\n"

    base = make_commit({"src.py": src, "tools/sv_check.py": two_cases})
    body = ("<!-- reviewer -->\nCloses #401\n- **Sweep SHA:** {sha}\n"
            "- **Sweep:** waived - loop/process\n"
            "- **Fail-before:** tools/sv_check.py 1 -> 2\n")
    files = [
        {"path": "src.py", "changeType": "MODIFIED", "additions": 2},
        {"path": "tools/sv_check.py", "changeType": "MODIFIED", "additions": 1},
    ]

    def verdict_for(head: str, key: str) -> tuple[str, str]:
        env, _ = with_fake_gh(prs={key: {
            "headRefOid": head, "baseRefOid": base,
            "body": body.format(sha=head[:7]), "files": files,
        }})
        return run(f"gh pr merge {key} --squash", role="coder", extra_env=env)

    head_rewritten = make_commit(
        {"src.py": src, "tools/sv_check.py": rewritten}, parent=base)
    verdict, reason = verdict_for(head_rewritten, "401")
    check("a rewritten expectation with no new case is refused", verdict, "refuse")
    check("refusal reports the case counts", "2 -> 2" in reason, True)

    head_added = make_commit(
        {"src.py": src, "tools/sv_check.py": three_cases}, parent=base)
    verdict, _ = verdict_for(head_added, "402")
    check("an added case is allowed", verdict, "allow")

    # A branch that forked before the base grew must not be read as a deletion.
    # Against the base tip this is 3 -> 2; against the fork point it is 2 -> 2,
    # and it is the difference between two refusals and ten across the last 25
    # merged PRs.
    moved_base = make_commit(
        {"src.py": src, "tools/sv_check.py": three_cases}, parent=base)
    env, _ = with_fake_gh(prs={"403": {
        "headRefOid": head_added, "baseRefOid": moved_base,
        "body": body.format(sha=head_added[:7]), "files": files,
    }})
    verdict, _ = run("gh pr merge 403 --squash", role="coder", extra_env=env)
    check("a branch forked before the base moved is measured at the fork",
          verdict, "allow")

    # Say nothing when the objects are not here, as the sweep gate does: an
    # unfetched branch must not become a refusal nobody can act on.
    env, _ = with_fake_gh(prs={"404": {
        "headRefOid": head_rewritten, "baseRefOid": "",
        "body": body.format(sha=head_rewritten[:7]), "files": files,
    }})
    verdict, _ = run("gh pr merge 404 --squash", role="coder", extra_env=env)
    check("an unresolvable fork point does not refuse", verdict, "allow")


def test_merge_may_not_close_a_needs_live_issue() -> None:
    """Gate 4. `Closes #N` on an issue that can only be checked live is refused,
    because closing it is what takes it out of every queue: of 178 issues closed
    here, 171 closed within ninety seconds of their merge."""
    env, _ = with_fake_gh(
        labels={"640": ["P1", "needs:live"], "641": ["P1"]},
        linked={"700": ["640"], "701": ["641"]},
    )
    verdict, reason = run("gh pr merge 700 --squash", role="coder", extra_env=env)
    check("a merge that would close a needs:live issue is refused", verdict, "refuse")
    check("the refusal names Refs #N", "Refs #640" in reason, True)

    verdict, _ = run("gh pr merge 701 --squash", role="coder", extra_env=env)
    check("a merge closing an ordinary issue is unaffected", verdict, "allow")

    # `Refs #N` leaves closingIssuesReferences empty, which is the whole point:
    # the field is populated from the closing keyword and from nothing else.
    env, _ = with_fake_gh(labels={"640": ["P1", "needs:live"]}, linked={"702": []})
    verdict, _ = run("gh pr merge 702 --squash", role="coder", extra_env=env)
    check("the same branch merges once the body says Refs", verdict, "allow")


def test_needs_live_close_requires_evidence() -> None:
    """The other end of gate 4: the issue stays open until somebody says where
    they saw the behaviour."""
    env, _ = with_fake_gh(labels={"640": ["P1", "needs:live", "stage:live"]})
    verdict, reason = run("gh issue close 640", role="reviewer", extra_env=env)
    check("closing a needs:live issue with a bare thread is refused", verdict, "refuse")
    check("the refusal names the line it wants", "live-verified:" in reason, True)

    env, _ = with_fake_gh(
        labels={"640": ["P1", "needs:live", "stage:live"]},
        comments={"640": ["<!-- tester -->\nRan it on the rig.\n\nlive-verified: raspberrypi.local 2026-09-14\n"]},
    )
    verdict, _ = run("gh issue close 640", role="reviewer", extra_env=env)
    check("a thread carrying the evidence line closes", verdict, "allow")

    # Taking the label off in the same command is the sanctioned way out, the
    # same shape as lifting `hold:monitor` and closing in one breath.
    env, _ = with_fake_gh(labels={"640": ["P1", "needs:live"]})
    verdict, _ = run(
        "gh issue edit 640 --remove-label needs:live && gh issue close 640",
        role="monitor", extra_env=env)
    check("dropping the label in the same command is allowed", verdict, "allow")

    env, _ = with_fake_gh(labels={"641": ["P1"]})
    verdict, _ = run("gh issue close 641", role="reviewer", extra_env=env)
    check("closing an ordinary issue is unaffected", verdict, "allow")


def test_needs_live_label_permissions() -> None:
    """Removing the marker is the bypass, so it is narrower than applying it."""
    env, _ = with_fake_gh()
    for role in ("reviewer", "tester", "monitor"):
        verdict, _ = run("gh issue edit 640 --add-label needs:live",
                         role=role, extra_env=env)
        check(f"{role} may mark an issue needs:live", verdict, "allow")

    verdict, _ = run("gh issue edit 640 --remove-label needs:live",
                     role="reviewer", extra_env=env)
    check("the reviewer may not take needs:live off", verdict, "refuse")

    verdict, _ = run("gh issue edit 640 --remove-label needs:live",
                     role="monitor", extra_env=env)
    check("the monitor may take needs:live off", verdict, "allow")

    verdict, _ = run("gh issue edit 640 --add-label stage:live --remove-label stage:pr",
                     role="coder", extra_env=env)
    check("the coder swaps stage:pr for stage:live at the merge", verdict, "allow")

    # Coder handback: swap stage:pr for stage:triage on refusal (#654)
    verdict, _ = run("gh issue edit 640 --remove-label stage:pr --add-label stage:triage",
                     role="coder", extra_env=env)
    check("the coder swaps stage:pr for stage:triage on handback", verdict, "allow")

    verdict, _ = run("gh issue edit 640 --remove-label stage:triage",
                     role="coder", extra_env=env)
    check("the coder cannot remove stage:triage", verdict, "refuse")

    verdict, _ = run("gh issue edit 640 --add-label stage:live", role="tester",
                     extra_env=env)
    check("the tester does not move issues into stage:live", verdict, "refuse")

    # stage:triage permissions for tester and aide (#654)
    verdict, _ = run("gh issue edit 1 --add-label stage:triage", role="tester")
    check("the tester may apply stage:triage", verdict, "allow")

    verdict, _ = run("gh issue edit 1 --remove-label stage:triage", role="tester")
    check("the tester may not remove stage:triage", verdict, "refuse")


def test_pr_merge_validates_sweep_breakdown_and_completeness() -> None:
    """Issue #655: Sweep provenance and breakdown completeness (Gate 2)."""
    commit = make_commit({"clean.py": "def foo() -> int:\n    return 42\n"})

    # 1. Stated sum does not match breakdown sum (PR #648 shape: 5353 green: sums to 5355)
    pr648_breakdown = (
        "5353 green: agenda 78 · browse 355 · cache 225 · citations 345 · deploy 762 · "
        "env 59 · history 282 · js 1183 · leak 52 · match 148 · models 26 · plan 419 · "
        "progress 22 · replay 69 · resolve 44 · runner 75 · screensaver 406 · "
        "sources 40 · subagent 55 · timing 121 · wake 74 · wiring 515"
    )
    env, _ = with_fake_gh(prs={"648": {
        "headRefOid": commit,
        "body": (f"<!-- reviewer -->\nCloses #648\n- **Sweep SHA:** {commit[:7]}\n"
                 f"- **Fail-before:** waived - owner ruling\n"
                 f"`{pr648_breakdown}`\n"),
        "files": [{"path": "clean.py", "changeType": "MODIFIED"}],
    }})
    verdict, reason = run("gh pr merge 648 --squash", role="coder", extra_env=env)
    check("PR #648 with mismatched breakdown sum is refused", verdict, "refuse")
    check("refusal mentions sum mismatch", "sweep breakdown sum does not match stated total" in reason, True)
    check("refusal mentions +2 diff", "+2" in reason, True)

    # 2. PR #649 shape: 5355 green: sums to 5358 (+3)
    pr649_breakdown = (
        "5355 green: agenda 78 · browse 355 · cache 225 · citations 345 · deploy 762 · "
        "env 59 · history 282 · js 1183 · leak 52 · match 148 · models 26 · plan 419 · "
        "progress 22 · replay 69 · resolve 44 · runner 75 · screensaver 394 · "
        "sources 40 · subagent 55 · timing 121 · wake 74 · wiring 530"
    )
    env, _ = with_fake_gh(prs={"649": {
        "headRefOid": commit,
        "body": (f"<!-- reviewer -->\nCloses #649\n- **Sweep SHA:** {commit[:7]}\n"
                 f"- **Fail-before:** waived - owner ruling\n"
                 f"`{pr649_breakdown}`\n"),
        "files": [{"path": "clean.py", "changeType": "MODIFIED"}],
    }})
    verdict, reason = run("gh pr merge 649 --squash", role="coder", extra_env=env)
    check("PR #649 with mismatched breakdown sum is refused", verdict, "refuse")
    check("refusal mentions +3 diff", "+3" in reason, True)

    suite_names = "agenda browse cache citations deploy env history js leak match models plan progress replay resolve runner screensaver sources subagent timing wake wiring".split()
    fixture_sha = make_commit({f"tools/{name}_check.py": "# fixture\n" for name in suite_names})

    # 3. Incomplete suites without waiver (PR #653 shape: 2 suites instead of 22)
    pr653_breakdown = "1750 green: js 1183 · wiring 567"
    env, _ = with_fake_gh(prs={"653": {
        "headRefOid": fixture_sha,
        "body": (f"<!-- reviewer -->\nCloses #653\n- **Sweep SHA:** {fixture_sha[:7]}\n"
                 f"- **Fail-before:** waived - owner ruling\n"
                 f"`{pr653_breakdown}`\n"),
        "files": [],
    }})
    verdict, reason = run("gh pr merge 653 --squash", role="coder", extra_env=env)
    check("PR #653 with 2/22 suites without waiver is refused", verdict, "refuse")
    check("refusal mentions incomplete suites", "reports 2 check suites" in reason, True)

    # 4. Incomplete suites with valid waiver is allowed
    env, _ = with_fake_gh(prs={"653w": {
        "headRefOid": fixture_sha,
        "body": (f"<!-- reviewer -->\nCloses #653\n- **Sweep SHA:** {fixture_sha[:7]}\n"
                 f"- **Sweep:** waived - loop/process\n"
                 f"- **Fail-before:** waived - owner ruling\n"
                 f"`{pr653_breakdown}`\n"),
        "files": [],
    }})
    verdict, _ = run("gh pr merge 653w --squash", role="coder", extra_env=env)
    check("PR with incomplete suites but valid waiver is allowed", verdict, "allow")

    # 5. Matching 22 suites and matching sum is allowed
    pr_matching = (
        "5343 green: agenda 78 · browse 355 · cache 225 · citations 345 · deploy 759 · "
        "env 59 · history 282 · js 1183 · leak 52 · match 148 · models 26 · plan 419 · "
        "progress 22 · replay 69 · resolve 44 · runner 75 · screensaver 394 · "
        "sources 40 · subagent 55 · timing 121 · wake 74 · wiring 518"
    )
    write_sweep_record(commit, pr_matching)
    env, _ = with_fake_gh(prs={"650": {
        "headRefOid": commit,
        "body": (f"<!-- reviewer -->\nCloses #650\n- **Sweep SHA:** {commit[:7]}\n"
                 f"- **Fail-before:** waived - owner ruling\n"
                 f"`{pr_matching}`\n"),
        "files": [{"path": "clean.py", "changeType": "MODIFIED"}],
    }})
    verdict, _ = run("gh pr merge 650 --squash", role="coder", extra_env=env)
    check("PR #650 with matching breakdown and 22 suites is allowed", verdict, "allow")

    # 6. Summary count without breakdown is refused (#689 / PR #668 shape)
    env, _ = with_fake_gh(prs={"668": {
        "headRefOid": fixture_sha,
        "body": (f"<!-- reviewer -->\nCloses #668\n- **Sweep SHA:** {fixture_sha[:7]}\n"
                 f"- **The sweep:** 22/22 green (2835 cases, wiring: 2.5s)\n"),
        "files": [],
    }})
    verdict, reason = run("gh pr merge 668 --squash", role="coder", extra_env=env)
    check("PR #668 with summary count but no breakdown is refused", verdict, "refuse")
    check("refusal mentions missing full suite breakdown", "missing the full suite breakdown" in reason, True)

    # 7. Fabricated check suites matching count but wrong names are refused (#689 / PR #675 shape)
    pr675_fake_breakdown = (
        "3979 green: agenda 78 · browse 355 · clock 10 · deploy 759 · "
        "env 59 · fetch 12 · fonts 8 · friction 5 · gws 50 · icons 14 · ir 20 · js 1183 · leak 52 · "
        "match 148 · models 26 · plan 419 · policy 15 · progress 22 · pulse 30 · "
        "runner 75 · timing 121 · wiring 518"
    )
    env, _ = with_fake_gh(prs={"675": {
        "headRefOid": fixture_sha,
        "body": (f"<!-- reviewer -->\nCloses #675\n- **Sweep SHA:** {fixture_sha[:7]}\n"
                 f"- **Fail-before:** waived - owner ruling\n"
                 f"`{pr675_fake_breakdown}`\n"),
        "files": [],
    }})
    verdict, reason = run("gh pr merge 675 --squash", role="coder", extra_env=env)
    check("PR #675 with fabricated suite names is refused", verdict, "refuse")
    check("refusal mentions suite names mismatch", "sweep breakdown suites do not match repository check scripts" in reason, True)
    check("refusal lists unknown suites", "unknown suites" in reason and "clock" in reason, True)

    # 8. All suites matching repository check scripts at PR head is allowed (#689)
    pr_real_breakdown = (
        "5343 green: agenda 78 · browse 355 · cache 225 · citations 345 · deploy 759 · "
        "env 59 · history 282 · js 1183 · leak 52 · match 148 · models 26 · plan 419 · "
        "progress 22 · replay 69 · resolve 44 · runner 75 · screensaver 394 · "
        "sources 40 · subagent 55 · timing 121 · wake 74 · wiring 518"
    )
    write_sweep_record(fixture_sha, pr_real_breakdown)
    env, _ = with_fake_gh(prs={"689_ok": {
        "headRefOid": fixture_sha,
        "body": (f"<!-- reviewer -->\nCloses #689\n- **Sweep SHA:** {fixture_sha[:7]}\n"
                 f"- **Fail-before:** waived - owner ruling\n"
                 f"`{pr_real_breakdown}`\n"),
        "files": [],
    }})
    verdict, _ = run("gh pr merge 689_ok --squash", role="coder", extra_env=env)
    check("PR with 22 matching suites at real head SHA is allowed", verdict, "allow")


def test_pr_merge_requires_sweep_record() -> None:
    """#738 Part 2: the `green:` line must match what the sweep recorded at the head.
    #741, #743 and #747 each gave a real 22/22 line measured on a dirty tree."""
    commit = make_commit({"clean.py": "def foo() -> int:\n    return 42\n"})
    line = (
        "5343 green: agenda 78 · browse 355 · cache 225 · citations 345 · deploy 759 · "
        "env 59 · history 282 · js 1183 · leak 52 · match 148 · models 26 · plan 419 · "
        "progress 22 · replay 69 · resolve 44 · runner 75 · screensaver 394 · "
        "sources 40 · subagent 55 · timing 121 · wake 74 · wiring 518"
    )

    def verdict_for(key: str, body_line: str, extra: str = "") -> tuple[str, str]:
        env, _ = with_fake_gh(prs={key: {
            "headRefOid": commit,
            "body": (f"<!-- reviewer -->\nCloses #{key}\n- **Sweep SHA:** {commit[:7]}\n"
                     f"- **Fail-before:** waived - owner ruling\n{extra}`{body_line}`\n"),
            "files": [{"path": "clean.py", "changeType": "MODIFIED"}],
        }})
        return run(f"gh pr merge {key} --squash", role="coder", extra_env=env)

    verdict, reason = verdict_for("751", line)
    check("a green line with no record at the head is refused", verdict, "refuse")
    check("refusal says there is no sweep record", "no sweep record" in reason, True)
    check("refusal says to commit before sweeping", "Commit first" in reason, True)

    write_sweep_record(commit, line.replace("js 1183", "js 1180").replace("5343", "5340"))
    verdict, reason = verdict_for("752", line)
    check("a green line that differs from the record is refused", verdict, "refuse")
    check("refusal names the differing suite", "js 1183 vs 1180" in reason, True)

    write_sweep_record(commit, line)
    verdict, _ = verdict_for("753", line)
    check("a green line matching the record is allowed", verdict, "allow")

    (TEST_SWEEP_RECORDS / f"{commit}.json").unlink()
    verdict, _ = verdict_for("754", line, "- **Sweep:** waived - loop/process\n")
    check("a sweep waiver needs no record", verdict, "allow")


def test_empty_stdin() -> None:
    out = raw("")
    check("empty stdin is allowed, not refused", "decision" in out, False)


def test_pr_create_gates() -> None:
    # 1. Missing Sweep SHA
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(
            "<!-- reviewer -->\n"
            "## Summary\n"
            "Testing gates.\n\n"
            "- **Fail-before:** tools/deploy_check.py 762 -> 763\n"
        )
        path = fh.name
    try:
        verdict, reason = run(f"gh pr create --title 'test' --body-file {path}", role="reviewer")
        check("pr create missing Sweep SHA is refused", verdict, "refuse")
        check("refusal names Sweep SHA", "Sweep SHA" in reason, True)
    finally:
        pathlib.Path(path).unlink(missing_ok=True)

    # 2. Missing Fail-before (no waiver)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(
            "<!-- reviewer -->\n"
            "## Summary\n"
            "Testing gates.\n\n"
            "- **Sweep SHA:** 1d408ff5d082a60aa1eb0dbb83ec9f56952d3d06\n"
        )
        path = fh.name
    try:
        verdict, reason = run(f"gh pr create --title 'test' --body-file {path}", role="reviewer")
        check("pr create missing Fail-before is refused", verdict, "refuse")
        check("refusal names Fail-before", "Fail-before" in reason, True)
    finally:
        pathlib.Path(path).unlink(missing_ok=True)

    # 3. PR #663 initial body (lacking both) is blocked
    pr_663_initial = (
        "<!-- reviewer -->\n"
        "Refs #660\n\n"
        "## Summary\n"
        "Restricts Cloud Run ALLOWED_HOSTS.\n\n"
        "## Verification\n"
        "- Before change: tools/deploy_check.py fails\n"
        "- After change: All 763 cases hold\n\n"
        "## Sweep\n"
        "- Base SHA: b77698190d1069f1981f92fc34fd0a1fa41dc035\n"
        "- 5394 green\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(pr_663_initial)
        path = fh.name
    try:
        verdict, reason = run(f"gh pr create --title 'fix' --body-file {path}", role="reviewer")
        check("PR #663 initial body is refused", verdict, "refuse")
    finally:
        pathlib.Path(path).unlink(missing_ok=True)

    # 4. PR #663 corrected body (having both) passes
    pr_663_corrected = (
        "<!-- reviewer -->\n"
        "Refs #660\n\n"
        "## Summary\n"
        "Restricts Cloud Run ALLOWED_HOSTS.\n\n"
        "## Verification\n"
        "- **Fail-before:** tools/deploy_check.py 762 -> 763\n\n"
        "## Sweep\n"
        "- **Sweep SHA:** 24d2b25da196d45904593f7d1b5b40581771c7e6\n"
        "- 5394 green\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(pr_663_corrected)
        path = fh.name
    try:
        verdict, reason = run(f"gh pr create --title 'fix' --body-file {path}", role="reviewer")
        check("PR #663 corrected body is allowed", verdict, "allow")
    finally:
        pathlib.Path(path).unlink(missing_ok=True)

    # 5. Draft carve-out: --draft or -d with body missing sweep/fail-before passes
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(
            "<!-- reviewer -->\n"
            "## Summary\n"
            "WIP draft PR without sweep or fail-before.\n"
        )
        path = fh.name
    try:
        verdict, _ = run(f"gh pr create --draft --title 'WIP' --body-file {path}", role="reviewer")
        check("gh pr create --draft skips sweep and fail-before checks", verdict, "allow")

        verdict, _ = run(f"gh pr create -d --title 'WIP' --body-file {path}", role="reviewer")
        check("gh pr create -d skips sweep and fail-before checks", verdict, "allow")
    finally:
        pathlib.Path(path).unlink(missing_ok=True)

    # 6. Each of the 4 fail-before waivers passes
    waivers = [
        "waived - needs: tester repro",
        "waived - loop/process",
        "waived - docs only",
        "waived - owner ruling",
    ]
    for w in waivers:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write(
                f"<!-- reviewer -->\n"
                f"## Summary\nTesting waiver.\n\n"
                f"- **Sweep SHA:** 1d408ff5d082a60aa1eb0dbb83ec9f56952d3d06\n"
                f"- **Fail-before:** {w}\n"
            )
            path = fh.name
        try:
            verdict, reason = run(f"gh pr create --title 'waiver test' --body-file {path}", role="reviewer")
            check(f"Fail-before {w} is allowed", verdict, "allow")
        finally:
            pathlib.Path(path).unlink(missing_ok=True)

    # 7. Flag variations: -F, -F=, --body-file=, --body, -b
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(pr_663_corrected)
        path = fh.name
    try:
        verdict, _ = run(f"gh pr create --title 'fix' -F {path}", role="reviewer")
        check("gh pr create -F is allowed", verdict, "allow")

        verdict, _ = run(f"gh pr create --title 'fix' -F={path}", role="reviewer")
        check("gh pr create -F= is allowed", verdict, "allow")

        verdict, _ = run(f"gh pr create --title 'fix' --body-file={path}", role="reviewer")
        check("gh pr create --body-file= is allowed", verdict, "allow")
    finally:
        pathlib.Path(path).unlink(missing_ok=True)

    # Inline --body and -b
    inline_body = "<!-- reviewer -->\\n- **Sweep SHA:** 1d408ff5d082a60aa1eb0dbb83ec9f56952d3d06\\n- **Fail-before:** waived - loop/process"
    verdict, _ = run(f'gh pr create --title "fix" --body "{inline_body}"', role="reviewer")
    check("gh pr create --body is allowed", verdict, "allow")

    verdict, _ = run(f'gh pr create --title "fix" -b "{inline_body}"', role="reviewer")
    check("gh pr create -b is allowed", verdict, "allow")

    # 8. Fail-before non-increasing count is refused
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(
            "<!-- reviewer -->\n"
            "## Summary\nEqual counts.\n\n"
            "- **Sweep SHA:** 1d408ff5d082a60aa1eb0dbb83ec9f56952d3d06\n"
            "- **Fail-before:** tools/deploy_check.py 762 -> 762\n"
        )
        path = fh.name
    try:
        verdict, reason = run(f"gh pr create --title 'fix' --body-file {path}", role="reviewer")
        check("Fail-before equal counts is refused", verdict, "refuse")
    finally:
        pathlib.Path(path).unlink(missing_ok=True)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    if FAILURES:
        print(f"{len(FAILURES)} failing:\n")
        for f in FAILURES:
            print(f"  - {f}\n")
        return 1
    print(f"all cases pass ({len(tests)} groups)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
