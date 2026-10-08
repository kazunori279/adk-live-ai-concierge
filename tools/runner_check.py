#!/usr/bin/env python3
"""Check the thing that runs the other checks.

`check_all.py` is the one script here whose failures are invisible by
construction: it reports on the suite, so anything it silently leaves out is
reported as green by the only thing anybody reads. Three ways that happens, all
of them one edit away:

- **A list instead of a glob.** The moment discovery is written down, an area
  added later is an area nobody sweeps, and the sweep line still reads as a
  full sweep because the total it prints is the total of what it ran. So the
  cases below run the real runner against directories of fabricated check
  scripts, whose names are ones the repo has never had — a frozen list finds
  none of them and the case fails, which a check against the real thirteen
  could not do.
- **A count nobody printed.** Ten of the thirteen stop on the first case that
  does not hold and never reach their closing line; the other three print
  `some of N cases failed` or `SOME CASES FAILED` instead of it. None of those
  is a number, and reading the last figure out of the output would take a
  count printed *before* the failure as the answer. So the parse is anchored,
  and the exit code has a veto over it.
- **A green line off a red run.** The sweep line exists to be pasted into a
  comment without editing, which means a run that lost an area must not print
  one, and neither must a subset — a subset's total looks exactly like a
  sweep's.

The runner is driven as a subprocess, from a temporary directory it has been
copied into, so that its own discovery reaches the fabricated scripts and not
this repo's. That also gets the exit codes checked as the shell sees them,
which is the whole of what makes it usable as a gate. The pure functions are
imported and called directly.

Nothing here runs a real check script. It talks to nothing and takes about a
second, most of which is one deliberate sleep.

    uv run python tools/runner_check.py

Exits non-zero on the first case that does not hold.
"""

import asyncio
import contextlib
import fcntl
import http.server
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import check_all  # noqa: E402
import tst_runner  # noqa: E402

# Counted rather than written down, so the closing line cannot drift from the
# number of cases.
RAN = 0


def check(name: str, got, want, detail: str = "") -> None:
    global RAN
    RAN += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail or got}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


def holds(n: int) -> str:
    """A check script that passes, in the shape all thirteen real ones end in."""
    return f"print('   ok   a case')\nprint('\\nall {n} cases hold')\n"


def stops(n: int = 0) -> str:
    """One that stops on a failing case, the way ten of the thirteen do.

    `n` prints a closing line *before* the failure, which is not a shape any
    real script produces but is exactly what a parse that ignored the exit code
    would read as a pass.
    """
    early = f"print('\\nall {n} cases hold')\n" if n else ""
    return ("import sys\n" + early
            + "print('   FAIL a case that does not hold')\nsys.exit(1)\n")


def quiet() -> str:
    """One that exits 0 and never says how many cases it held."""
    return "print('nothing to report')\n"


def slow() -> str:
    """One that takes long enough for the seconds column to have to be real."""
    return "import time\ntime.sleep(0.4)\n" + holds(5)


@contextlib.contextmanager
def fabricated(**areas: str):
    """A tools directory holding a copy of the runner and the given scripts.

    The runner discovers from its own `__file__`, so copying it in is what
    points it at these instead of at the repo — no flag, and nothing in
    `check_all.py` that exists only for this script.
    """
    with tempfile.TemporaryDirectory() as tmp:
        made = Path(tmp)
        shutil.copy(TOOLS / "check_all.py", made / "check_all.py")
        for name, body in areas.items():
            (made / f"{name}_check.py").write_text(body, encoding="utf-8")
        yield made


@contextlib.contextmanager
def fabricated_git_repo(**areas: str):
    """A git repository holding a project directory with copied runner and checks."""
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        project_dir = repo_root / "2026-0906-raspi-ai-terminal"
        tools_dir = project_dir / "tools"
        tools_dir.mkdir(parents=True)
        git_env = {k: v for k, v in os.environ.items() if k not in check_all._GIT_SCRUB}
        subprocess.run(["git", "init"], cwd=str(repo_root), capture_output=True, check=True, env=git_env)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(repo_root), capture_output=True, check=True, env=git_env)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_root), capture_output=True, check=True, env=git_env)
        shutil.copy(TOOLS / "check_all.py", tools_dir / "check_all.py")
        for name, body in areas.items():
            (tools_dir / f"{name}_check.py").write_text(body, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(repo_root), capture_output=True, check=True, env=git_env)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_root), capture_output=True, check=True, env=git_env)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_root),
                              capture_output=True, text=True, check=True, env=git_env).stdout.strip()
        yield tools_dir, repo_root, head


def swept(made: Path, *argv: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run the copied runner and return its exit code and everything it said."""
    sub_env = {k: v for k, v in os.environ.items() if k not in check_all._GIT_SCRUB}
    if env:
        sub_env.update(env)
    done = subprocess.run([sys.executable, str(made / "check_all.py"), *argv],
                          capture_output=True, text=True, env=sub_env)
    return done.returncode, done.stdout + done.stderr


# The line as it is pasted into the comments, kept whole rather than assembled
# out of pieces: the two characters that carry it are an arrow and a middle
# dot, and a hyphen or an ASCII bullet in either place is a diff nobody reads.
HOUSE = (
    "1032 → 1035 green: agenda 16 · browse 194 · cache 55 · env 39 · "
    "history 52 · js 162 · match 126 · plan 159 · progress 22 · resolve 44 · "
    "subagent 44 · timing 96 · wiring 26"
)
HOUSE_COUNTS = {
    "agenda": 16, "browse": 194, "cache": 55, "env": 39, "history": 52,
    "js": 162, "match": 126, "plan": 159, "progress": 22, "resolve": 44,
    "subagent": 44, "timing": 96, "wiring": 26,
}


def discovery_cases() -> None:
    print("== what a sweep finds ==")

    # Listed a second way, off the directory rather than through the glob, so
    # that a discovery narrowed to a subset of what is there fails here.
    beside = sorted(n for n in os.listdir(TOOLS) if n.endswith("_check.py"))
    found = [p.name for p in check_all.discovered()]
    check("every check script in tools/ is in the sweep", found, beside,
          f"{len(found)} scripts")
    check("including this one, which was not there when the runner was written",
          "runner_check.py" in found, True)
    check("the runner does not sweep itself",
          "check_all.py" in found, False)
    check("nor anything else in tools/ that is not a check",
          [n for n in found if n in ("gws_bench.py", "latency_report.py")], [])

    # Names the repo has never had. A discovery written down as a list of the
    # thirteen finds nothing here, which is the point of running it on a
    # directory rather than on tools/.
    with fabricated(zzz=holds(1), aaa=holds(2)) as made:
        names = [check_all.area(p) for p in check_all.discovered(made)]
        check("a directory of areas this repo has never had is swept whole",
              names, ["aaa", "zzz"])
        check("in the order the sweep line names them, not the order found",
              names, sorted(names))
        check("and the copy of the runner sitting beside them is not one",
              [p.name for p in check_all.discovered(made)
               if "check_all" in p.name], [])

    check("an area's name is its file without the suffix",
          check_all.area(Path("/x/tools/agenda_check.py")), "agenda")


def count_cases() -> None:
    print("\n== the count off a run ==")

    check("the closing line is the count", check_all.held(
        "   ok   a case\n\nall 96 cases hold\n"), 96)
    check("env and match spell a failure differently, and it is not a count",
          check_all.held("   FAIL a case\n\nsome of 52 cases failed\n"), None)
    check("browse spells it differently again",
          check_all.held("\nSOME CASES FAILED\n"), None)
    check("a script that stopped on its first failure never printed one",
          check_all.held("   ok   a case\n   FAIL another\n"), None)
    # Every case name these scripts print is indented three spaces, so the
    # anchor is what keeps a case *about* this line from being read as it —
    # including the ones in this script.
    check("a case named after the line is not the line", check_all.held(
        "   ok   all 3 cases hold is what it prints: True\n"), None)
    check("the last one wins, for a script that prints more than one",
          check_all.held("all 1 cases hold\nall 2 cases hold\n"), 2)


def sweep_line_cases() -> None:
    print("\n== the line that gets pasted ==")

    check("the sweep line is the house line, character for character",
          check_all.sweep(HOUSE_COUNTS, 1032), HOUSE)
    check("the total is added up, not passed in",
          check_all.sweep(HOUSE_COUNTS, 1032).split(" green:")[0].split(" → ")[1],
          "1035")
    check("with no earlier total, the line starts at this one",
          check_all.sweep({"agenda": 16, "browse": 194}),
          "210 green: agenda 16 · browse 194")


def green_cases() -> None:
    print("\n== a run where everything holds ==")

    with fabricated(b=holds(7), a=holds(3), c=holds(2)) as made:
        code, out = swept(made)
        check("a clean sweep exits zero", code, 0)
        check("and ends on a line that can be pasted as it is",
              out.strip().splitlines()[-1],
              "12 green: a 3 · b 7 · c 2")
        code, out = swept(made, "--before", "10")
        check("--before is the only figure it is told",
              out.strip().splitlines()[-1],
              "10 → 12 green: a 3 · b 7 · c 2")

    with fabricated(a=holds(3), slow=slow()) as made:
        code, out = swept(made)
        seconds = re.search(r"^slow\s+5 cases\s+([\d.]+)s$", out, re.M)
        check("every area reports its own wall time", bool(seconds), True,
              seconds.group(0) if seconds else out)
        check("and a slow one is visibly the slow one",
              float(seconds.group(1)) >= 0.4, True, f"{seconds.group(1)}s")


def red_cases() -> None:
    print("\n== a run where something does not ==")

    with fabricated(a=holds(3), b=stops(), c=holds(2)) as made:
        code, out = swept(made)
        check("one area down takes the exit code with it", code, 1)
        check("the areas that held still report their counts",
              [line.split()[1] for line in out.splitlines()
               if line.startswith(("a ", "c "))], ["3", "2"])
        check("the one that did not is named with its code",
              "b" in out and "exit 1" in out, True)
        check("with its last lines, so the failing case is on screen",
              "FAIL a case that does not hold" in out, True)
        check("and the command to see the rest of them",
              "uv run python tools/b_check.py" in out, True)
        check("no sweep line comes off a red run", " green: " in out, False)
        check("and it says how much of the sweep is missing",
              "1 of 3 areas did not hold" in out, True)

    with fabricated(a=holds(3), b=quiet()) as made:
        code, out = swept(made)
        check("exiting zero without a count is a failure too", code, 1)
        check("said as what it is, rather than as a crash",
              "no count line" in out, True)

    # The shape that a parse without the exit-code veto reads as a pass: a
    # closing line printed, and then the script falls over.
    with fabricated(a=holds(3), b=stops(9)) as made:
        code, out = swept(made)
        check("a count printed before the crash is still not a pass", code, 1)
        check("and its figure is nowhere in the totals", " green: " in out, False)


def subset_cases() -> None:
    print("\n== one area at a time ==")

    with fabricated(a=holds(3), b=holds(7), c=holds(2)) as made:
        code, out = swept(made, "-c", "b")
        check("a subset runs what it was asked for", code, 0)
        check("and nothing else",
              [line.split()[0] for line in out.splitlines()
               if line and line[0].isalpha() and "cases" in line], ["b"])
        check("a subset prints no sweep line", " green: " in out, False)
        check("it says what it covered instead",
              "7 cases hold in 1 of 3 areas" in out, True)

        code, out = swept(made, "-c", "b", "-c", "c")
        check("the flag repeats, the way -q does on the benches",
              "9 cases hold in 2 of 3 areas" in out, True, code)

        code, out = swept(made, "-c", "nope")
        check("an area that does not exist is refused", code, 2)
        check("by name, with the ones that do listed",
              "invalid choice: 'nope'" in out and "a, b, c" in out, True)

        code, out = swept(made, "-c", "b", "--before", "10")
        check("and --before is refused on a subset rather than believed",
              code, 2, out.strip().splitlines()[-1])

    with fabricated(a=holds(3), b=stops()) as made:
        code, _ = swept(made, "-c", "b")
        check("a subset is a gate too", code, 1)


def exclusion_cases() -> None:
    print("\n== the sweep minus an area ==")

    with fabricated(a=holds(3), b=holds(7), c=holds(2), zzz=holds(5)) as made:
        code, out = swept(made, "--except", "b")
        check("excluding an area runs the glob minus that one", code, 0)
        run_areas = [line.split()[0] for line in out.splitlines()
                     if line and line[0].isalpha() and "cases" in line]
        check("and picks up a newly added area without editing any list",
              run_areas, ["a", "c", "zzz"])
        check("an excluded run prints no sweep line", " green: " in out, False)
        check("it says it was a subset and names what was not run",
              "10 cases hold in 3 of 4 areas, b not run" in out, True)

        code, out = swept(made, "--except", "b", "-x", "zzz")
        check("the exclusion flag repeats",
              "5 cases hold in 2 of 4 areas, b, zzz not run" in out, True)

        code, out = swept(made, "--except", "nope")
        check("excluding an area that does not exist is refused", code, 2)
        check("by name, with the ones that do listed",
              "invalid choice: 'nope'" in out and "a, b, c, zzz" in out, True)

        code, out = swept(made, "--except", "b", "--before", "10")
        check("and --before is refused on an exclusion subset",
              code, 2, out.strip().splitlines()[-1])

        code, out = swept(made, "-c", "a", "--except", "b")
        check("combining -c and --except is refused", code, 2)
        check("saying they cannot be combined",
              "cannot combine -c/--check and --except" in out, True)

    with fabricated(a=holds(3), b=stops(), c=holds(2)) as made:
        code, _ = swept(made, "--except", "b")
        check("excluding a failing area leaves the sweep green", code, 0)
        code, _ = swept(made, "--except", "a")
        check("an excluded sweep fails when an included area fails", code, 1)


def mapping_cases() -> None:
    print("\n== path mapping ==")

    discovered = [check_all.area(s) for s in check_all.discovered()]
    mapped = {a for _, areas in check_all.PATH_AREAS for a in areas}
    missing = set(discovered) - mapped
    check("every discovered check area appears in PATH_AREAS", missing, set(), f"{len(discovered)} areas")

    matched, unmapped = check_all.resolve_paths(["src/ai_terminal/live/static/js/app.js"])
    check("app.js resolves to js and citations", matched, {"js", "citations"})
    check("app.js has no unmapped paths", unmapped, [])

    matched, unmapped = check_all.resolve_paths(["unmapped/file.xyz"])
    check("unmapped path returns empty areas", matched, set())
    check("unmapped path returns in unmapped list", unmapped, ["unmapped/file.xyz"])


def path_subset_cases() -> None:
    print("\n== subset by path ==")

    with fabricated(js=holds(3), citations=holds(7), wiring=holds(2)) as made:
        code, out = swept(made, "--for", "src/ai_terminal/live/static/js/app.js")
        check("--for runs only areas claiming the path", code, 0)
        check("--for prints no sweep line", " green: " in out, False)
        check("--for reports coverage",
              "10 cases hold in 2 of 3 areas" in out, True, out.strip().splitlines()[-1])

        code, out = swept(made, "--for", "unmapped/file.xyz")
        check("unmapped path falls back to full sweep", code, 0)
        check("unmapped path emits warning",
              "check_all.py: warning: unmapped path 'unmapped/file.xyz', falling back to full sweep" in out, True)
        check("unmapped path runs all areas",
              "12 green: citations 7 · js 3 · wiring 2" in out, True)

        code, out = swept(made, "--for", "src/ai_terminal/live/static/js/app.js", "-c", "js")
        check("combining --for and -c is refused", code, 2)
        check("refusal message names the conflict",
              "cannot combine --for/--for-diff and -c/--except" in out, True)

        code, out = swept(made, "--for", "src/ai_terminal/live/static/js/app.js", "--except", "js")
        check("combining --for and --except is refused", code, 2)
        check("refusal message names the conflict",
              "cannot combine --for/--for-diff and -c/--except" in out, True)

        code, out = swept(made, "--for", "src/ai_terminal/live/static/js/app.js", "--before", "10")
        check("combining --for and --before is refused", code, 2)
        check("refusal message names the conflict",
              "--before belongs to a full sweep, not a subset" in out, True)

    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        env = {k: v for k, v in os.environ.items() if k not in getattr(check_all, "_GIT_SCRUB", ())}
        subprocess.run(["git", "init"], cwd=repo_root, capture_output=True, check=True, env=env)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_root, capture_output=True, check=True, env=env)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, capture_output=True, check=True, env=env)
        tools = repo_root / "tools"
        tools.mkdir()
        shutil.copy(TOOLS / "check_all.py", tools / "check_all.py")
        (tools / "js_check.py").write_text(holds(3), encoding="utf-8")
        (tools / "citations_check.py").write_text(holds(7), encoding="utf-8")
        (tools / "wiring_check.py").write_text(holds(2), encoding="utf-8")
        (repo_root / "README.md").write_text("hello", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo_root, capture_output=True, check=True, env=env)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_root, capture_output=True, check=True, env=env)

        code, out = swept(tools, "--for-diff", "HEAD")
        check("--for-diff with empty diff exits zero", code, 0)
        check("--for-diff with empty diff reports no changed files",
              "no changed files against HEAD" in out, True)

        js_dir = repo_root / "src/ai_terminal/live/static/js"
        js_dir.mkdir(parents=True)
        (js_dir / "app.js").write_text("// test", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo_root, capture_output=True, check=True, env=env)
        subprocess.run(["git", "commit", "-m", "add app.js"], cwd=repo_root, capture_output=True, check=True, env=env)

        code, out = swept(tools, "--for-diff", "HEAD~1")
        check("--for-diff runs covering areas", code, 0)
        check("--for-diff reports subset count",
              "10 cases hold in 2 of 3 areas" in out, True)


class _MockServerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _write_log(self, msg: str):
        log_path = getattr(self.server, "log_path", None)
        if log_path:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"INFO: {msg}\n")

    def do_GET(self):
        if self.path == "/api/config":
            status = getattr(self.server, "config_status", 200)
            payload = getattr(self.server, "config_payload", {})
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self._write_log(f"127.0.0.1 - GET {self.path} {status} OK")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.headers.get("Content-Length"):
            length = int(self.headers["Content-Length"])
            self.rfile.read(length)
        if self.path == "/api/wake":
            status = getattr(self.server, "wake_status", 200)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            self._write_log(f"127.0.0.1 - POST {self.path} {status} OK")
        else:
            self.send_response(404)
            self.end_headers()


@contextlib.contextmanager
def mock_rig_server(server_log: Path | None = None, auto_log: bool = True):
    with tempfile.TemporaryDirectory() as tmp_dir:
        target_log = server_log or (Path(tmp_dir) / "tst-server.log")
        if auto_log and not target_log.exists():
            target_log.write_text("INFO ai_terminal.live.main: running revision rev123\n", encoding="utf-8")

        orig_server_log = tst_runner.SERVER_LOG
        tst_runner.SERVER_LOG = str(target_log)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockServerHandler)
        server.config_status = 200
        server.config_payload = {"management_routes": True, "revision": "rev123"}
        server.wake_status = 200
        server.log_path = target_log if auto_log else None
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, f"http://127.0.0.1:{port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)
            tst_runner.SERVER_LOG = orig_server_log


def unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tst_runner_preflight_cases() -> None:
    print("\n== tst_runner preflight ==")

    # 1. Unreachable server
    port = unused_port()
    ok, err = tst_runner.server_preflight(f"http://127.0.0.1:{port}")
    check("preflight fails on unreachable server", ok, False)
    check("unreachable server error message", err, f"server unreachable at http://127.0.0.1:{port}")

    with mock_rig_server() as (server, base_url):
        # 2. Management routes disabled in config payload
        server.config_payload = {"management_routes": False, "revision": "rev123"}
        server.wake_status = 200
        ok, err = tst_runner.server_preflight(base_url)
        check("preflight fails when management_routes is False", ok, False)
        check("management_routes False error message", err, "server up but ENABLE_MANAGEMENT_ROUTES is off")

        # 3. POST /api/wake returns 403
        server.config_payload = {"management_routes": True, "revision": "rev123"}
        server.wake_status = 403
        ok, err = tst_runner.server_preflight(base_url)
        check("preflight fails when wake returns 403", ok, False)
        check("wake 403 error message", err, "server up but ENABLE_MANAGEMENT_ROUTES is off")

        # 4. POST /api/wake returns 404
        server.config_payload = {"management_routes": True, "revision": "rev123"}
        server.wake_status = 404
        ok, err = tst_runner.server_preflight(base_url)
        check("preflight fails when wake returns 404", ok, False)
        check("wake 404 error message", err, "server up but ENABLE_MANAGEMENT_ROUTES is off")

        # 5. Revision mismatch
        server.config_payload = {"management_routes": True, "revision": "rev123"}
        server.wake_status = 200
        ok, err = tst_runner.server_preflight(base_url, expected_sha="rev999")
        check("preflight fails on revision mismatch", ok, False)
        check("revision mismatch error message", err, "server revision 'rev123' does not match HEAD 'rev999'")

        # 6. Preflight passes with matching revision and active log
        ok, err = tst_runner.server_preflight(base_url, expected_sha="rev123")
        check("preflight passes with matching revision", ok, True)
        check("matching revision message ok", err, "ok")

        # 7. Preflight passes without expected_sha
        ok, err = tst_runner.server_preflight(base_url, expected_sha=None)
        check("preflight passes without expected_sha", ok, True)
        check("no expected_sha message ok", err, "ok")

        # 8. Preflight fails when server_log does not exist (#724)
        if server.log_path and server.log_path.exists():
            server.log_path.unlink()
        ok, err = tst_runner.server_preflight(base_url)
        check("preflight fails when server_log missing", ok, False)
        check("server_log missing error message", err, f"server is not logging to {tst_runner.SERVER_LOG} (start with tools/tst_server.sh)")

        # 9. Preflight fails when server_log has mismatched revision (#724)
        server.log_path.write_text("INFO ai_terminal.live.main: running revision rev999\n", encoding="utf-8")
        ok, err = tst_runner.server_preflight(base_url)
        check("preflight fails when server_log revision mismatches", ok, False)
        check("server_log revision mismatch error message", err, f"server is not logging to {tst_runner.SERVER_LOG} (start with tools/tst_server.sh)")

        # 10. Preflight fails when server is not writing to server_log (#724)
        server.log_path.write_text("INFO ai_terminal.live.main: running revision rev123\n", encoding="utf-8")
        server.log_path = None  # disable logging on requests
        ok, err = tst_runner.server_preflight(base_url)
        check("preflight fails when server does not write to log", ok, False)
        check("server not writing to log error message", err, f"server is not logging to {tst_runner.SERVER_LOG} (start with tools/tst_server.sh)")

    # 11. tst_runner aborts and appends zero metrics on preflight failure
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        mock_metrics = tmp_path / "metrics.jsonl"
        mock_repo_metrics = tmp_path / "repo_metrics.jsonl"
        orig_metrics = tst_runner.METRICS_FILE
        orig_repo_metrics = tst_runner.REPO_METRICS_FILE
        orig_base_url = tst_runner.BASE_URL
        try:
            tst_runner.METRICS_FILE = str(mock_metrics)
            tst_runner.REPO_METRICS_FILE = mock_repo_metrics
            tst_runner.BASE_URL = f"http://127.0.0.1:{unused_port()}"
            exit_code = None
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    asyncio.run(tst_runner.main())
                except SystemExit as exc:
                    exit_code = exc.code
            check("tst_runner aborts on preflight failure", exit_code, 1)
            metrics_written = mock_metrics.exists() or mock_repo_metrics.exists()
            check("zero metrics appended on preflight failure", metrics_written, False)
        finally:
            tst_runner.METRICS_FILE = orig_metrics
            tst_runner.REPO_METRICS_FILE = orig_repo_metrics
            tst_runner.BASE_URL = orig_base_url


def tst_runner_run_lock_cases() -> None:
    print("\n== tst_runner run lock ==")
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_lock = Path(tmp_dir) / "tst-runner.lock"

        # 1. Fresh lock succeeds and writes PID
        ok, msg, fd = tst_runner.acquire_run_lock(test_lock)
        holder_pid = test_lock.read_text(encoding="utf-8").strip() if test_lock.exists() else ""
        tst_runner.release_run_lock(fd)

        # Stale lock succeeds
        ok_stale, msg_stale, fd_stale = tst_runner.acquire_run_lock(test_lock)
        tst_runner.release_run_lock(fd_stale)

        check("acquire_run_lock succeeds on fresh/stale lock, writes PID",
              ok and ok_stale and holder_pid == str(os.getpid()), True)

        # 2. When lock is held by active fd, second acquire_run_lock fails with PID message
        ok1, _, fd1 = tst_runner.acquire_run_lock(test_lock)
        ok2, err2, fd2 = tst_runner.acquire_run_lock(test_lock)
        tst_runner.release_run_lock(fd1)
        check("second acquire_run_lock fails when lock is held", ok2, False)
        check("lock held error message names holder PID", err2,
              f"tst_runner already running (PID {os.getpid()}); exiting without running tests")

    # 3. Full runner invocation aborts with code 1 and writes zero metrics when lock is held
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        mock_metrics = tmp_path / "metrics.jsonl"
        mock_repo_metrics = tmp_path / "repo_metrics.jsonl"
        mock_lock = tmp_path / "runner.lock"

        orig_metrics = tst_runner.METRICS_FILE
        orig_repo_metrics = tst_runner.REPO_METRICS_FILE
        orig_lock = tst_runner.RUN_LOCK_FILE

        # Hold lock with another fd
        lock_fd = os.open(str(mock_lock), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(lock_fd, f"{os.getpid()}\n".encode("utf-8"))

        try:
            tst_runner.METRICS_FILE = str(mock_metrics)
            tst_runner.REPO_METRICS_FILE = mock_repo_metrics
            tst_runner.RUN_LOCK_FILE = mock_lock
            exit_code = None
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    asyncio.run(tst_runner.main())
                except SystemExit as exc:
                    exit_code = exc.code
            check("tst_runner aborts when run lock is held", exit_code, 1)
            metrics_written = mock_metrics.exists() or mock_repo_metrics.exists()
            check("zero metrics appended when run lock is held", metrics_written, False)
        finally:
            os.close(lock_fd)
            tst_runner.METRICS_FILE = orig_metrics
            tst_runner.REPO_METRICS_FILE = orig_repo_metrics
            tst_runner.RUN_LOCK_FILE = orig_lock


def tst_trajectories_expected_tools_cases() -> None:
    print("\n== tst_trajectories expected_tools matching (#729) ==")
    import tst_trajectories

    check("exact matching tools satisfy list",
          tst_trajectories.check_expected_tools(["web_search", "maps_search"], ["web_search", "maps_search"]),
          True)
    check("subset missing tool fails",
          tst_trajectories.check_expected_tools(["web_search", "maps_search"], ["web_search"]),
          False)
    check("choice group satisfied by first option",
          tst_trajectories.check_expected_tools(["web_search", ["maps_search", "place_info"]], ["web_search", "maps_search"]),
          True)
    check("choice group satisfied by second option",
          tst_trajectories.check_expected_tools(["web_search", ["maps_search", "place_info"]], ["web_search", "place_info"]),
          True)
    check("choice group fails when neither matches",
          tst_trajectories.check_expected_tools(["web_search", ["maps_search", "place_info"]], ["web_search", "weather"]),
          False)


def tst_trajectories_working_selector_cases() -> None:
    print("\n== tst_trajectories working indicator selector (#735) ==")
    traj_src = (TOOLS / "tst_trajectories.py").read_text(encoding="utf-8")
    check("execute_turn queries .working class selector",
          "page.query_selector(\".working\")" in traj_src,
          True)
    check("execute_turn does not query obsolete #working id",
          "page.query_selector(\"#working\")" not in traj_src,
          True)


def sweep_record_cases() -> None:
    print("\n== sweep record provenance (#738, #748) ==")
    with tempfile.TemporaryDirectory() as tmp_rec:
        rec_dir = Path(tmp_rec) / "sweep_records"
        env = {"SWEEP_RECORD_DIR": str(rec_dir)}

        with fabricated_git_repo(a=holds(3), b=holds(7)) as (tools_dir, repo_root, head):
            rec_file = rec_dir / f"{head}.json"

            # 1. Green full sweep writes record whose areas equal counts and sha matches HEAD
            code, out = swept(tools_dir, env=env)
            check("green full sweep exits zero", code, 0)
            check("green full sweep writes record", rec_file.is_file(), True)
            check("green full sweep prints record path", str(rec_file) in out, True)
            if rec_file.is_file():
                data = json.loads(rec_file.read_text(encoding="utf-8"))
                check("record sha matches HEAD", data.get("sha"), head)
                check("record areas match counts", data.get("areas"), {"a": 3, "b": 7})
                check("record total matches sum", data.get("total"), 10)
                check("record has started_at", isinstance(data.get("started_at"), str) and bool(data.get("started_at")), True)
                check("record has finished_at", isinstance(data.get("finished_at"), str) and bool(data.get("finished_at")), True)
            else:
                check("record sha matches HEAD", None, head)
                check("record areas match counts", None, {"a": 3, "b": 7})
                check("record total matches sum", None, 10)
                check("record has started_at", False, True)
                check("record has finished_at", False, True)

            # 2. Subset run writes no record and stays silent on sweep record
            if rec_file.exists():
                rec_file.unlink()
            code, out = swept(tools_dir, "-c", "a", env=env)
            check("subset run exits zero", code, 0)
            check("subset run writes no record", rec_file.exists(), False)
            check("subset run does not mention sweep record", "sweep record" in out, False)

            # 3. Run with dirty file outside project dir still writes record and prints path
            (repo_root / "outside_dirty.txt").write_text("outside", encoding="utf-8")
            if rec_file.exists():
                rec_file.unlink()
            code, out = swept(tools_dir, env=env)
            check("sweep with dirty file outside project dir writes record", rec_file.is_file(), True)
            check("sweep with dirty file outside project dir prints record path", str(rec_file) in out, True)

            # 4. Run with dirty files under project dir writes no record and prints reason
            (tools_dir / "dirty_inside.txt").write_text("inside", encoding="utf-8")
            if rec_file.exists():
                rec_file.unlink()
            code, out = swept(tools_dir, env=env)
            check("sweep with dirty file inside project dir writes no record", rec_file.exists(), False)
            check("sweep with dirty file inside project dir prints sweep record not written", "sweep record not written" in out, True)
            dirty_lines = [l for l in out.splitlines() if "sweep record not written" in l]
            check("dirty sweep reason mentions dirty", bool(dirty_lines) and "dirty" in dirty_lines[0], True)

            # 5. Red full sweep prints sweep record not written with red count
            (tools_dir / "dirty_inside.txt").unlink()
            (tools_dir / "b_check.py").write_text(stops(), encoding="utf-8")
            if rec_file.exists():
                rec_file.unlink()
            code, out = swept(tools_dir, env=env)
            check("red full sweep exits 1", code, 1)
            check("red full sweep writes no record", rec_file.exists(), False)
            check("red full sweep prints sweep record not written", "sweep record not written" in out, True)
            red_lines = [l for l in out.splitlines() if "sweep record not written" in l]
            check("red full sweep reason mentions red count", bool(red_lines) and "1 of 2 areas red" in red_lines[0], True)

            # 6. Run outside git repository prints not on a git commit
            with fabricated(a=holds(2)) as non_git:
                code, out = swept(non_git, env=env)
                check("full sweep outside git exits zero", code, 0)
                check("full sweep outside git prints not on a git commit", "sweep record not written: not on a git commit" in out, True)


def main() -> None:
    discovery_cases()
    count_cases()
    sweep_line_cases()
    green_cases()
    red_cases()
    subset_cases()
    exclusion_cases()
    mapping_cases()
    path_subset_cases()
    tst_runner_preflight_cases()
    tst_runner_run_lock_cases()
    tst_trajectories_expected_tools_cases()
    tst_trajectories_working_selector_cases()
    sweep_record_cases()
    print(f"\nall {RAN} cases hold")


if __name__ == "__main__":
    main()


