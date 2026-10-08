#!/usr/bin/env python3
"""Run every check script in this directory and total what they hold.

Until this there was no runner, so a sweep was thirteen commands typed out one
after another and a line of arithmetic done by eye afterwards. Both halves fail
quietly. A script left out of the sweep is a green report over an area nobody
ran, and the one most likely to be left out is the one added last — which is
also the one whose cases are newest. A total added up by hand drifts by exactly
the amount that would have shown the omission.

So the list is a glob, not a list: a new `tools/<area>_check.py` joins the
sweep by existing, and the figure on each line is the script's own closing
count rather than one typed in beside it. Every script ends on

    all N cases hold

and the parse is anchored to the whole line, because the case names those
scripts print are indented and a case named after this line should not be
mistaken for it. A script that exits non-zero, or exits zero without printing
that line, is reported as a failure and takes the run's exit code with it:
"crashed" and "printed no count" are the same thing here, since neither of them
is a number anybody can add up. Only the tail of a failing script is shown —
enough to see which case went, with the command to re-run it in full.

    uv run python tools/check_all.py                  # the whole sweep
    uv run python tools/check_all.py --before 1032    # ... with the arrow
    uv run python tools/check_all.py -c cache -c js   # one area, or a few
    uv run python tools/check_all.py --except env     # the sweep minus an area
    uv run python tools/check_all.py --for <path>     # only areas covering path
    uv run python tools/check_all.py --for-diff <ref> # areas covering git diff

The last form is for whoever touched one area: run the script that owns it and
leave the sweep to whoever is reviewing. A subset prints no sweep line, because
a sweep line is a claim about the whole suite and a subset's total looks
exactly like one.

`--before` is the only figure here this cannot work out for itself: it belongs
to a tree that no longer exists by the time the sweep runs. Reading it back out
of the last run would have to survive two sessions sweeping at once and a
subset overwriting the total with a partial one, so it is passed in instead,
and without it the line simply starts at the total.

One after another rather than at once, which would halve the wall time and cost
more than it saved: `progress_check` asserts the shape of a timed run — notes
falling either side of `QUIET` and `GAP`, scaled down by a tenth — and `cache`
and `browse` are timed in the same way. Sharing four cores with `env`'s dozen
subprocesses is exactly the contention that turns those into a suite that fails
once a fortnight, and a check nobody trusts is worse than a slow one.

The slow areas are visible in the seconds column rather than in a comment. Four
of them are 85 of the sweep's 97 seconds — `env` 42 on its own, then `cache`,
`progress` and `browse` — and a script that joins them there should be noticed
while it is still new, rather than after the sweep has quietly become the thing
nobody runs.

Before running any script, the runner emits provenance details: sys.executable,
the project root, the resolved realpath of ai_terminal.__file__, and git HEAD
SHA with dirty status. At completion, it verifies that git HEAD has not moved
and that no untracked or modified files were left behind by any check script.
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent

SWEEP_RECORD_DIR = Path(os.environ.get("SWEEP_RECORD_DIR", "/tmp/ai-terminal-sweep"))

PATH_AREAS: list[tuple[str, tuple[str, ...]]] = [
    # Top-level & deploy & repo configs
    ("Dockerfile", ("deploy", "citations")),
    (".dockerignore", ("deploy", "citations")),
    ("deploy/*", ("deploy", "citations")),
    ("README.md", ("deploy", "models", "citations")),
    (".env.example", ("env", "citations")),
    ("pyproject.toml", ("citations",)),
    ("uv.lock", ("citations",)),
    (".gitignore", ("citations",)),
    (".claude/**", ("citations", "deploy")),
    (".agents/**", ("citations", "deploy")),

    # Client / static
    ("src/ai_terminal/live/static/js/*", ("js", "citations")),
    ("src/ai_terminal/live/static/index.html", ("js", "citations")),
    ("src/ai_terminal/live/static/css/*", ("js", "browse", "citations")),

    # Server core
    ("src/ai_terminal/__init__.py", ("citations",)),
    ("src/ai_terminal/wake.py", ("wake", "deploy", "leak", "citations")),
    ("src/ai_terminal/live/__init__.py", ("citations",)),
    ("src/ai_terminal/live/_log.py", ("leak", "citations")),
    ("src/ai_terminal/live/agent.py", ("wiring", "citations")),
    ("src/ai_terminal/live/browser.py", ("browse", "wiring", "leak", "citations")),
    ("src/ai_terminal/live/conn.py", ("history", "wiring", "citations")),
    ("src/ai_terminal/live/main.py", ("deploy", "history", "wiring", "leak", "citations")),
    ("src/ai_terminal/live/memory.py", ("history", "leak", "citations")),
    ("src/ai_terminal/live/recap.py", ("history", "wiring", "leak", "citations")),
    ("src/ai_terminal/live/screen.py", ("wiring", "progress", "leak", "citations")),
    ("src/ai_terminal/live/screensaver.py", ("screensaver", "deploy", "leak", "citations")),

    # Tools
    ("src/ai_terminal/live/tools/__init__.py", ("wiring", "citations")),
    ("src/ai_terminal/live/tools/_agenda.py", ("agenda", "wiring", "leak", "citations")),
    ("src/ai_terminal/live/tools/_client.py", ("env", "models", "wiring", "citations")),
    ("src/ai_terminal/live/tools/_gws_cli.py", ("agenda", "subagent", "wiring", "leak", "citations")),
    ("src/ai_terminal/live/tools/_gws_run.py", ("agenda", "plan", "subagent", "cache", "wiring", "leak", "citations")),
    ("src/ai_terminal/live/tools/_match.py", ("match", "citations")),
    ("src/ai_terminal/live/tools/_plan_run.py", ("plan", "replay", "history", "wiring", "leak", "citations")),
    ("src/ai_terminal/live/tools/_progress.py", ("progress", "browse", "wiring", "leak", "citations")),
    ("src/ai_terminal/live/tools/_sdm.py", ("wiring", "leak", "citations")),
    ("src/ai_terminal/live/tools/_sources.py", ("sources", "wiring", "citations")),
    ("src/ai_terminal/live/tools/_timing.py", ("timing", "history", "wiring", "leak", "citations")),
    ("src/ai_terminal/live/tools/browse.py", ("browse", "history", "wiring", "leak", "citations")),
    ("src/ai_terminal/live/tools/calc.py", ("wiring", "citations")),
    ("src/ai_terminal/live/tools/camera.py", ("wiring", "citations")),
    ("src/ai_terminal/live/tools/gws.py", ("cache", "resolve", "subagent", "history", "wiring", "leak", "citations")),
    ("src/ai_terminal/live/tools/gws_cache.py", ("cache", "resolve", "screensaver", "history", "wiring", "leak", "citations")),
    ("src/ai_terminal/live/tools/home.py", ("subagent", "wiring", "citations")),
    ("src/ai_terminal/live/tools/image_search.py", ("match", "wiring", "citations")),
    ("src/ai_terminal/live/tools/maps.py", ("plan", "wiring", "citations")),
    ("src/ai_terminal/live/tools/place.py", ("history", "wiring", "citations")),
    ("src/ai_terminal/live/tools/plan.py", ("plan", "replay", "history", "wiring", "citations")),
    ("src/ai_terminal/live/tools/weather.py", ("browse", "screensaver", "history", "wiring", "leak", "citations")),
    ("src/ai_terminal/live/tools/youtube_search.py", ("match", "wiring", "citations")),

    # Skills & checks & tools
    ("src/ai_terminal/live/skills/**", ("citations",)),
    ("tools/check_all.py", ("runner", "citations")),
    ("tools/runner_check.py", ("runner", "citations")),
    ("tools/*_check.py", ("citations",)),
    ("tools/*_bench.py", ("citations",)),
    ("tools/cloud_run_deploy.py", ("deploy", "citations")),
    ("tools/*.sh", ("deploy", "citations")),
    ("tools/*.py", ("citations",)),
    ("tools/*.yml", ("deploy", "citations")),
    ("spikes/**", ("browse", "citations")),
    ("training/**", ("wake", "citations")),
]


def resolve_paths(paths: list[str], root: Path = ROOT) -> tuple[set[str], list[str]]:
    matched: set[str] = set()
    unmapped: list[str] = []
    root_resolved = root.resolve()

    for raw in paths:
        clean = Path(raw).as_posix().lstrip("./")
        hits = {a for pat, areas in PATH_AREAS if fnmatch.fnmatch(clean, pat) for a in areas}
        if not hits:
            try:
                p = Path(raw)
                resolved = (Path.cwd() / p).resolve() if not p.is_absolute() else p.resolve()
                rel = resolved.relative_to(root_resolved).as_posix()
                hits = {a for pat, areas in PATH_AREAS if fnmatch.fnmatch(rel, pat) for a in areas}
            except (ValueError, RuntimeError):
                pass

        if hits:
            matched.update(hits)
        else:
            unmapped.append(raw)

    return matched, unmapped


_GIT_SCRUB = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
              "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")


def _git(args: list[str], root: Path = ROOT) -> str | None:
    """Run a git command in `root`, returning stdout or None on failure."""
    try:
        env = {k: v for k, v in os.environ.items() if k not in _GIT_SCRUB}
        done = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, env=env)
        if done.returncode == 0:
            return done.stdout
    except OSError:
        pass
    return None


def git_head(root: Path = ROOT) -> str | None:
    """The current HEAD commit SHA, or None if not in a git repository."""
    out = _git(["rev-parse", "HEAD"], root=root)
    return out.strip() if out is not None else None


def git_status(root: Path = ROOT) -> str | None:
    """The porcelain status output, or None if not in a git repository."""
    return _git(["status", "--porcelain"], root=root)


def git_project_status(root: Path = ROOT) -> str | None:
    """The porcelain status output under `root`, or None if not in a git repository."""
    return _git(["status", "--porcelain", "--", "."], root=root)


def resolve_ai_terminal(root: Path = ROOT) -> str:
    """Resolve the realpath of imported `ai_terminal.__file__`."""
    src = root / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        import ai_terminal
        return os.path.realpath(ai_terminal.__file__)
    except Exception as exc:
        return f"<not imported: {exc}>"


def verify_git_integrity(head_before: str | None, status_before: str | None,
                         root: Path = ROOT) -> list[str]:
    """Check that git HEAD and working tree status were not modified."""
    if head_before is None:
        return []
    problems: list[str] = []
    head_after = git_head(root)
    if head_after != head_before:
        problems.append(f"git HEAD moved: {head_before} -> {head_after}")
    status_after = git_status(root)
    if status_after is not None and status_before is not None:
        before_lines = set(status_before.splitlines())
        after_lines = status_after.splitlines()
        new_or_modified = [line for line in after_lines if line not in before_lines]
        if new_or_modified:
            problems.append(
                "untracked or modified files left behind:\n"
                + "\n".join(f"  {line}" for line in new_or_modified)
            )
        elif status_after != status_before:
            problems.append("git working tree status changed during run")
    return problems


# The closing line of every check script, anchored at both ends.
HELD = re.compile(r"^all (\d+) cases hold$", re.M)

# How much of a failing script's output to show. Ten of the thirteen stop on
# the first case that does not hold, so their last lines are the failure; the
# three that run to the end print a count of what failed on the last line.
TAIL = 15


def discovered(tools: Path = TOOLS) -> list[Path]:
    """Every check script in `tools`, in the order the sweep line names them.

    A glob rather than a list, so that adding an area is one file and no edit
    here. `check_all.py` does not end in `_check.py` and so does not find
    itself, which is the one name this must not return.
    """
    return sorted(tools.glob("*_check.py"))


def area(script: Path) -> str:
    """`agenda_check.py` -> `agenda`, the name the sweep line uses."""
    return script.name.removesuffix("_check.py")


def held(output: str) -> int | None:
    """The case count a run ended on, or None if it did not end on one."""
    found = HELD.findall(output)
    return int(found[-1]) if found else None


def ran(script: Path) -> tuple[int | None, float, int, str]:
    """Run one script: its count (None if it did not hold), seconds, code, output.

    Run through `sys.executable` rather than another `uv run`, so that thirteen
    scripts resolve the environment once between them instead of thirteen
    times, and so that a sweep started in a particular interpreter stays in it.
    """
    started = time.monotonic()
    done = subprocess.run([sys.executable, str(script)], cwd=ROOT,
                          capture_output=True, text=True)
    seconds = time.monotonic() - started
    output = done.stdout + done.stderr
    count = held(output) if done.returncode == 0 else None
    return count, seconds, done.returncode, output


def sweep(counts: dict[str, int], before: int | None = None) -> str:
    """The line that goes in the comment, ready to paste.

    `1032 -> 1035 green: agenda 16 · browse 194 · ...`, or the same without the
    arrow when there is no earlier total to count up from.
    """
    total = sum(counts.values())
    head = f"{total}" if before is None else f"{before} → {total}"
    return (f"{head} green: "
            + " · ".join(f"{name} {n}" for name, n in counts.items()))


def write_sweep_record(
    sha: str,
    counts: dict[str, int],
    started_at: str,
    finished_at: str,
    record_dir: Path | None = None,
) -> Path:
    """Atomically write a sweep provenance record JSON file."""
    if record_dir is None:
        if SWEEP_RECORD_DIR != Path(os.environ.get("SWEEP_RECORD_DIR", "/tmp/ai-terminal-sweep")):
            record_dir = SWEEP_RECORD_DIR
        elif "SWEEP_RECORD_DIR" in os.environ:
            record_dir = Path(os.environ["SWEEP_RECORD_DIR"])
        else:
            record_dir = SWEEP_RECORD_DIR
    record_dir = Path(record_dir)
    record_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "sha": sha,
        "areas": counts,
        "total": sum(counts.values()),
        "started_at": started_at,
        "finished_at": finished_at,
    }
    target_path = record_dir / f"{sha}.json"
    temp_file = tempfile.NamedTemporaryFile(
        mode="w",
        dir=record_dir,
        delete=False,
        encoding="utf-8",
        prefix="sweep_",
        suffix=".tmp",
    )
    try:
        json.dump(record, temp_file, indent=2)
        temp_file.write("\n")
        temp_file.flush()
        os.fsync(temp_file.fileno())
        temp_file.close()
        os.replace(temp_file.name, target_path)
    except Exception:
        if os.path.exists(temp_file.name):
            try:
                os.unlink(temp_file.name)
            except OSError:
                pass
        raise
    return target_path


def main(argv=None) -> int:
    started_at = datetime.now(timezone.utc).isoformat()
    project_status_before = git_project_status(ROOT)
    project_dirty_before = bool(project_status_before.strip()) if project_status_before is not None else False

    scripts = discovered()
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("-c", "--check", action="append",
                        choices=[area(s) for s in scripts],
                        help="only this area; repeatable")
    parser.add_argument("-x", "--except", "--exclude", dest="exclude",
                        action="append", choices=[area(s) for s in scripts],
                        help="exclude this area; repeatable")
    parser.add_argument("--before", type=int,
                        help="the total the sweep line counts up from")
    parser.add_argument("--for", dest="for_paths", action="append", default=[],
                        help="only areas covering this path; repeatable")
    parser.add_argument("--for-diff",
                        help="only areas covering files changed since git ref")
    args = parser.parse_args(argv)
    if not scripts:
        parser.error(f"no *_check.py in {TOOLS}")
    if (args.for_paths or args.for_diff) and (args.check or args.exclude):
        parser.error("cannot combine --for/--for-diff and -c/--except")
    if (args.for_paths or args.for_diff) and args.before is not None:
        parser.error("--before belongs to a full sweep, not a subset")
    if args.check and args.exclude:
        parser.error("cannot combine -c/--check and --except")
    if (args.check or args.exclude) and args.before is not None:
        parser.error("--before belongs to a full sweep, not a subset")

    paths: list[str] = list(args.for_paths)
    if args.for_diff:
        env = {k: v for k, v in os.environ.items() if k not in _GIT_SCRUB}
        res = subprocess.run(["git", "-C", str(ROOT), "diff", "--name-only", args.for_diff],
                             capture_output=True, text=True, env=env)
        if res.returncode != 0:
            sys.stderr.write(res.stderr)
            return res.returncode
        diff_files = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        if not diff_files and not args.for_paths:
            print(f"no changed files against {args.for_diff}")
            return 0
        paths.extend(diff_files)

    if args.for_paths or args.for_diff:
        matched, unmapped = resolve_paths(paths, ROOT)
        if unmapped:
            for u in unmapped:
                print(f"check_all.py: warning: unmapped path '{u}', falling back to full sweep", file=sys.stderr)
            chosen = scripts
        else:
            chosen = [s for s in scripts if area(s) in matched]
            if not chosen:
                parser.error("no checks mapped for given paths")
    else:
        exclude = set(args.exclude or [])
        chosen = [
            s for s in scripts
            if (not args.check or area(s) in args.check) and area(s) not in exclude
        ]
        if not chosen:
            parser.error("all areas excluded")

    is_full_sweep = (
        not args.check
        and not args.exclude
        and not args.for_paths
        and not args.for_diff
        and len(chosen) == len(scripts)
    )

    ai_terminal_path = resolve_ai_terminal(ROOT)
    head_before = git_head(ROOT)
    status_before = git_status(ROOT)
    dirty_before = bool(status_before.strip()) if status_before is not None else False

    print(f"sys.executable: {sys.executable}")
    print(f"root:           {ROOT}")
    print(f"ai_terminal:    {ai_terminal_path}")
    if head_before is not None:
        status_flag = "dirty" if dirty_before else "clean"
        print(f"git:            {head_before} ({status_flag})")
    print()

    width = max(len(area(s)) for s in chosen)
    counts: dict[str, int] = {}
    failed: list[tuple[Path, int, str]] = []

    for script in chosen:
        # Printed before the run and finished after it, so a sweep that is
        # thirteen seconds into `env` is not thirteen seconds of silence.
        print(f"{area(script):<{width}} ", end="", flush=True)
        count, seconds, code, output = ran(script)
        if count is None:
            why = f"exit {code}" if code else "no count line"
            print(f"{why:>12}  {seconds:>5.1f}s")
            failed.append((script, code, output))
        else:
            print(f"{count:>6} cases  {seconds:>5.1f}s")
            counts[area(script)] = count

    for script, code, output in failed:
        lines = output.rstrip().splitlines()
        print(f"\n== {script.name}, exit {code} ==")
        print("\n".join(lines[-TAIL:]))
        print(f"   uv run python tools/{script.name}")

    git_problems = verify_git_integrity(head_before, status_before, ROOT)
    if git_problems:
        print("\n== git tree integrity violations ==")
        for problem in git_problems:
            print(f"   {problem}")

    if failed:
        # No sweep line on a red run: the point of the line is that it can be
        # pasted without editing, and a partial one is a green claim.
        if is_full_sweep:
            print(f"\nsweep record not written: {len(failed)} of {len(chosen)} areas red")
        print(f"\n{len(failed)} of {len(chosen)} areas did not hold")
        return 1
    if git_problems:
        if is_full_sweep:
            print(f"\nsweep record not written: {len(git_problems)} git tree integrity violation(s) detected")
        print(f"\n{len(git_problems)} git tree integrity violation(s) detected")
        return 1
    if args.check:
        print(f"\n{sum(counts.values())} cases hold in "
              f"{len(chosen)} of {len(scripts)} areas")
        return 0
    if args.exclude:
        omitted = ", ".join(sorted(exclude))
        print(f"\n{sum(counts.values())} cases hold in "
              f"{len(chosen)} of {len(scripts)} areas, {omitted} not run")
        return 0
    if len(chosen) < len(scripts):
        print(f"\n{sum(counts.values())} cases hold in "
              f"{len(chosen)} of {len(scripts)} areas")
        return 0

    is_clean = (
        project_status_before is not None
        and not project_dirty_before
        and not git_problems
    )
    has_valid_head = head_before is not None and len(head_before) == 40

    if is_full_sweep and is_clean and has_valid_head:
        finished_at = datetime.now(timezone.utc).isoformat()
        rec_path = write_sweep_record(head_before, counts, started_at, finished_at)
        print(f"\nsweep record: {rec_path}")
    elif is_full_sweep:
        if not has_valid_head:
            print("\nsweep record not written: not on a git commit")
        elif project_dirty_before:
            print("\nsweep record not written: project tree dirty at start")
        elif project_status_before is None:
            print("\nsweep record not written: could not determine git status")

    print(f"\n{sweep(counts, args.before)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
