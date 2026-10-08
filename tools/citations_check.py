"""Check that a `file.py:123` written in a comment still points at something.

This tree argues in its comments, and the argument is held together by line
citations: `gws.py:880` declines without a CLI, `style.css:44` is where the
560 px comes from, `weather.py:211-214` is the polar-day branch. Between them
they are the only index this codebase has. Nothing enforces them, and a line
number is a coordinate into a file that moves — every insertion above a cited
line silently walks the citation off its subject, and the comment goes on
reading as though somebody had just checked.

Measured at `a928e73`, 43 of the 67 in-repo citations named something other
than what their sentence said, and `tools/plan_bench.py` was wrong at all
seven of its own. A cleanup without a gate is that number back inside a month,
so this is the gate, and the cleanup is what the gate caught.

What it can decide, and it decides it from the citation as written rather than
from a list kept beside it:

- **The file is one file in this repo.** A bare `README.md` that matches two
  is a citation the reader cannot resolve either, so it fails rather than
  quietly picking the shallower one. `spikes/` is out of the tree for this
  purpose: it is a vendored experiment, not this project's source, and its
  `README.md` is the only reason the root one is ambiguous at all.
- **The range is inside the file.** The weakest rule here and the one that
  almost never fires, because drift is a few lines and files are long. It is
  in because it is free and because a deletion big enough to trip it is the
  one case where the citation is certainly wrong.
- **A citation this repo does not own resolves in the environment.** A third
  of the citations here name `google-genai`, ADK, starlette or Playwright,
  and those are checked only for existing, by basename, under this
  interpreter's `site-packages`. It is thin, and it is here so that a name
  nothing answers to is not silently skipped: a rename inside `src/` sends
  every citation of the old name down this branch, and most of them stop
  here. Not all. A few of this tree's basenames — `main.py` and `_client.py`
  — are also names something in `site-packages` carries, so a rename of one
  of those would pass. The branch narrows the hole; it does not close it.

And one thing it reports without gating on: **whether the first and last lines
of the range say something**. Nobody points a sentence at a blank line, or at
the `\"\"\"` that closes somebody else's docstring, so a citation that lands on
one has drifted — and drift lands there often, because blank lines are where
paragraphs of code end and where the gap a citation slid across usually stops.
This is the rule that finds things: it caught 20 of the 67 citations that were
stale at `a928e73`, and reading all 20 found a real defect behind every one.

It is a report rather than a case because of what it costs as one. Measured,
inserting a single line at the top of the body of `main.py`, `screensaver.py`
or `plan.py` turns one to four of these red, and turning the sweep red on an
ordinary insertion charges every change in this tree for a rule whose green a
number bump restores without restoring truth — a bump onto a different wrong
line is indistinguishable from a fix. So it prints what it found, exits zero,
and the cleanup it drives is a chore somebody runs rather than a toll on
whoever last edited a cited file.

And a second, for the case that rule is blind to: **where the cited lines
went**. A blank line catches a citation whose target was deleted, because
whatever slid up into its place is almost always non-blank — and sliding is
the ordinary way a citation goes stale as a file grows above it. So this asks
the question the other way round. `git blame` says which commit last wrote the
citing sentence; that commit's copy of the cited file says what the sentence
was looking at; and the file today says whether that text is still there, has
moved, or is gone. Nothing has to be written beside the citation for it to
work, so it reaches every citation already in this tree rather than the ones
somebody remembered to mark up. An anchor token per citation was the other way
to do this and it was measured instead: 65 of the 79 in-repo citing lines are
already 70 to 79 characters wide, so writing one in reflows the comment around
it, and a reflow moves every citation below it in that file. The cure would
have manufactured the disease.

This one gates against a ratchet baseline: the count cannot exceed what
was measured at introduction. It needs history (`git blame`) to know where
a line went, so the gate only fires in a git checkout with history. In an
extraction without history (`git archive`), citations are counted out of
reach and the case holds. `relocate` pure cases decide it everywhere.

**Run it on a committed tree, or the count of what it could not reach is your
own edits.** The blame is of the working tree, so a citing line nobody has
committed answers with forty zeros and is counted as out of reach rather than
measured against whoever last committed something else there. Editing above a
citation does it too. Commit and run again; nothing is broken in between.

What none of this can decide is whether the cited line ever meant what the
sentence around it claims. `browser.py:610` is
`launch["executable_path"] = chromium` and the sentence next to it says
Playwright never looks on PATH; no mechanical rule gets from one to the other.
A citation that was pointing at the wrong line the day somebody wrote it reads
as held here, because nothing moved. Reading is what catches those.

Which is also why **the report is a list of citations to go and read, not a
list of numbers to paste in**. It answers where the line that used to be here
went, and the sentence wanted where its subject is now. Those coincide when the
citation was exact and part company when it was a line or two off — the
commonest kind of near-miss — so a reported number is where to start reading.

Files in this project and nothing else. Issue bodies, review comments and
commit messages are where most of this tree's `file:line` prose is written and
none of it is swept, so a citation is only as checked as the file it sits in.

    uv run python tools/citations_check.py

Exits non-zero on the first case that does not hold, including moved
citations above baseline. A run finding only blank lines exits zero.
"""

import os
import re
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_GIT_SCRUB = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
              "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")


def _git_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_SCRUB}

RAN = 0


def check(name: str, got, want, detail: str = "") -> None:
    global RAN
    RAN += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail or got!r}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


# ------------------------------------------------------ what a citation is

# A filename this tree could plausibly own or cite, a colon, and a line or a
# range. The extension list is what the rest of this repo is written in plus
# what it cites out of its dependencies; a suffix nobody has ever cited is
# left off rather than guessed at, because every one added is a new way for a
# version string or a timestamp to read as a citation.
SUFFIXES = ("py", "js", "css", "html", "md", "json", "toml", "sh", "service",
            "conf", "yml", "yaml", "txt")

# The leading guard is what keeps `_gaos/types/interactions/model.py:53` from
# matching twice, once whole and once from `model.py`, and what keeps the
# `.py` inside a longer word out.
CITE = re.compile(
    r"(?<![\w./-])([A-Za-z_][\w./-]*\.(?:" + "|".join(SUFFIXES) + r"))"
    r":(\d+)(?:\s*-\s*(\d+))?")

# Everything in the tree that can hold a comment. Binary and generated files
# are left out by suffix rather than by name: `uv.lock` is the only large
# generated text file here and it has no suffix on this list.
READ = {".py", ".js", ".md", ".css", ".html", ".toml", ".sh", ".service",
        ".conf", ".yml", ".json", ".example", ".txt"}

# `spikes/` is a vendored experiment kept for its notes. Nothing in `src/`
# imports it, nothing cites into it, and its own `README.md` is the only
# reason a bare one is ambiguous. `.venv` is checked, but through
# `site-packages` below and not as part of this tree.
#
# The rest are gitignored state that lives beside the checkout rather than in
# it: the owner's exported Workspace documents, the picture behind the clock,
# the household's voice takes, and the Pi's cloud-init. Sweeping them makes
# the count a property of one machine rather than of the commit, and a
# citation landing in one turns the sweep red for a file nobody else has.
SKIP = {".git", ".venv", "node_modules", "__pycache__", "spikes",
        "gws_cache", "screensaver", "recordings", "cloud-init"}

# The same, for the two that are files and not directories.
# `current_issues.md` is rewritten every half hour out of GitHub issue text,
# which in this repo is dense with citations; `custom.toml` holds the account
# password hash and the Wi-Fi credentials and has no business being opened.
SKIP_NAMES = {"current_issues.md", "custom.toml"}


def spelled(name: str, first: int, last: int | None = None) -> str:
    """One citation, assembled rather than written out.

    Every fixture below is a `<file>:<line>` string, and this file is swept by
    the same pass as every other file in the tree. A fixture written as a
    literal would be read as a citation in its own right and would have to
    name something real; built from its parts it is invisible to the sweep,
    which is what lets the cases below name files that do not exist.
    """
    return f"{name}:{first}" + (f"-{last}" if last is not None else "")


def cited(text: str) -> list[tuple[str, int, int]]:
    """Every citation in one string, as (name, first line, last line)."""
    found = []
    for match in CITE.finditer(text):
        first = int(match.group(2))
        last = int(match.group(3)) if match.group(3) else first
        found.append((match.group(1), first, last))
    return found


BLANK = re.compile(r"^[\s)\]}(\[{\"'`,;:*/<>+#|-]*$")


def says_nothing(line: str) -> bool:
    """Whether a line could not be what a sentence is pointing at.

    Punctuation and nothing else: a blank, the `\"\"\"` that ends a docstring,
    a lone `)` closing a call whose name is four lines up, the `*/` under a
    JSDoc block, a `|` from a markdown table's blank row. None of these is
    ever the subject of a sentence, so a citation whose range starts or ends
    on one is pointing a few lines away from whatever it means.

    Deliberately generous about what counts as punctuation, and deliberately
    silent about the *inside* of a range: a citation may well span a blank
    line, and a rule that objected would fire on most of the correct ones.
    """
    return bool(BLANK.match(line))


# ------------------------------------------------------ where one may point
WORKSPACE = ROOT
GIT_ROOT = WORKSPACE if (WORKSPACE / ".git").exists() else ROOT


def tree(root: Path = ROOT) -> list[Path]:
    """Every file in this project and skills/hooks a citation may name or be written in."""
    paths: list[Path] = [
        path for path in root.rglob("*")
        if path.is_file() and path.suffix in READ
        and path.name not in SKIP_NAMES
        and not SKIP & set(path.relative_to(root).parts)
    ]
    if root == ROOT:
        for sub in (WORKSPACE / ".claude" / "skills",
                    WORKSPACE / ".claude" / "hooks",
                    WORKSPACE / ".agents" / "skills"):
            if sub.is_dir():
                for path in sub.rglob("*"):
                    if (path.is_file() and path.suffix in READ
                            and path.name not in SKIP_NAMES
                            and not SKIP & set(path.parts)):
                        paths.append(path)
    seen: set[Path] = set()
    deduped: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            deduped.append(path)
    return sorted(deduped)


def index(paths: list[Path], root: Path = ROOT) -> dict[str, list[Path]]:
    """Every way one of those files could be named, to the files it names.

    Every suffix of the path and not only the basename, because the tree
    writes both — `gws.py` and `skills/gws-shared/SKILL.md` are cited in the
    same file. A name that reaches two files is left holding two, so the
    ambiguity is the caller's to fail on rather than this function's to
    resolve by picking one.
    """
    found: dict[str, list[Path]] = {}
    for path in paths:
        if path.is_relative_to(root):
            parts = path.relative_to(root).parts
            for start in range(len(parts)):
                found.setdefault("/".join(parts[start:]), []).append(path)
        elif path.is_relative_to(WORKSPACE):
            parts = path.relative_to(WORKSPACE).parts
            for start in range(len(parts)):
                key = "/".join(parts[start:])
                if start == len(parts) - 1 and key == "README.md":
                    continue
                found.setdefault(key, []).append(path)
    return found


def installed() -> set[str]:
    """The basenames of every module under this interpreter's packages.

    Basenames, because two thirds of the dependency citations here are written
    that way — `trustedhost.py:28`, `runners.py:2007-2011` — and there is no
    honest way to resolve one to a path. So this answers "is there a file by
    this name in the environment", which is all a bare name can be asked, and
    the rest of what a dependency citation claims is out of reach from here.
    """
    lib = Path(sysconfig.get_paths()["purelib"])
    return {path.name for path in lib.rglob("*")
            if path.is_file() and path.suffix in (".py", ".js")}


def verdict(target: Path, first: int, last: int) -> str:
    """Why this range is not in the file at all, or `""` if it is.

    Both ends, and both directions. `drifted` subscripts on `first` and on
    `last`, so a range this lets through as "inside the file" has to be one
    that can be indexed: `:0` reads the last line instead of the first and
    says nothing about it, and `:999999-2` passes the one test there used to
    be here and then raises `IndexError` a line later, under an `ok`.
    """
    lines = target.read_text(encoding="utf-8").splitlines()
    if first < 1:
        return "line numbers start at 1"
    if first > last:
        return f"the range runs backwards from line {first}"
    if last > len(lines):
        return f"the file stops at line {len(lines)}"
    return ""


def drifted(target: Path, first: int, last: int) -> str:
    """Why this range does not look pointed at, or `""` if it does.

    The reported rule rather than the gated one, so it is a function of its
    own: everything above answers "is this citation resolvable", and this
    answers "does it look like somebody checked recently". Assumes the range
    is inside the file, which `verdict` has already said.
    """
    lines = target.read_text(encoding="utf-8").splitlines()
    if says_nothing(lines[first - 1]):
        return f"line {first} is {lines[first - 1].strip() or 'blank'!r}"
    if says_nothing(lines[last - 1]):
        return f"line {last} is {lines[last - 1].strip() or 'blank'!r}"
    return ""


# ------------------------------------------------------ where a range went

# The shortest line worth searching a whole file for. Below it a key is `)` or
# `armSession();` or `# does not.` — text a file holds in a dozen places,
# where a match is a coincidence rather than the same code seen again.
# Measured over the 79 in-repo citations here, 12 leaves exactly one of them
# undecidable, and that one was pointing at a blank line the day it was
# written.
FLOOR = 12

# What `relocate` says when the range it was handed has no line in it worth
# searching a file for. Named rather than spelled out at both ends, because
# the sweep has to tell "did not move" from "could not tell" and a report that
# folded the second into the first would be the quiet kind of green.
UNSURE = "nothing distinctive to search for"

# The ratchet baseline: the count of relocated citations measured when the
# gate was introduced. A citation repaired in its citing sentence moves its
# blame to the repair commit and drops off the report, so this count only falls;
# any newly drifted citation pushes the count above BASELINE and fails.
BASELINE = 165


def relocate(was: list[str], now: list[str], first: int) -> str:
    """Where the lines a citation used to name are now, or `""` if unmoved.

    `was` is the cited range as it read when the citing sentence was last
    written, `now` is the whole cited file today, and `first` is the line the
    citation names. The longest line of the range is the key, because the
    longest line is the one least likely to be in two places at once, and the
    rest of the range rides along on its offset.

    The point of this is that it has three answers where the blank-line rule
    has two. A citation whose subject moved and a citation whose subject was
    deleted look identical to `drifted` — both land on some other line that
    says something — and they want opposite fixes: a new number, or a new
    sentence. Here the first is `moved to N` and the second is `gone`.
    """
    if not was:
        return ""
    keyed = max(range(len(was)), key=lambda i: len(was[i].strip()))
    key = was[keyed].strip()
    if len(key) < FLOOR:
        return f"{UNSURE} at line {first + keyed}"
    # Indentation is not part of the key: a range that moved into or out of a
    # `with` or an `if` is the same range, and a citation of it is still good.
    anchor = first - 1 + keyed
    if 0 <= anchor < len(now) and now[anchor].strip() == key:
        return ""
    hits = [i for i, line in enumerate(now) if line.strip() == key]
    if not hits:
        return f"gone from the file: {key[:48]!r}"
    # The nearest, because a range that moved has usually moved a little, and
    # the count is printed so that a reader can distrust the number when the
    # key turned out to be less distinctive than its length suggested.
    best = min(hits, key=lambda i: abs(i - anchor))
    crowd = f", 1 of {len(hits)} places it reads" if len(hits) > 1 else ""
    return f"moved to {best - keyed + 1}{crowd}: {key[:48]!r}"


def history(*args: str, root: Path = ROOT) -> str:
    """One read of this checkout's history, or `""` if there is not one."""
    try:
        done = subprocess.run(("git", "-C", str(root), *args),
                              capture_output=True, text=True, env=_git_env())
    except OSError:
        return ""
    return done.stdout if done.returncode == 0 else ""


def wrote(rel: str, line: int, root: Path = ROOT) -> str:
    """The commit that last wrote this line, or `""` if none did.

    The working tree and not `HEAD`, so that the line number asked about is
    the one the sweep just read. A line nobody has committed blames to forty
    zeros, which no commit answers to, and the citation on it is reported as
    out of reach rather than measured against somebody else's line.
    """
    out = history("blame", "--porcelain", "-L", f"{line},{line}", "--", rel,
                  root=root)
    sha = out.split(" ", 1)[0] if out else ""
    return "" if set(sha) <= {"0"} else sha


def snapshot(sha: str, rel: str, root: Path = ROOT) -> list[str] | None:
    """The cited file as that commit left it, or `None` if it had none."""
    out = history("show", f"{sha}:./{rel}", root=root)
    return out.splitlines() if out else None


# --------------------------------------------------- the reader's own cases

def shape_cases() -> None:
    print("== what reads as a citation and what does not ==")

    check("a file and a line", cited(spelled("gws.py", 830)),
          [("gws.py", 830, 830)])
    check("a range is its two ends", cited(spelled("weather.py", 211, 214)),
          [("weather.py", 211, 214)])
    check("a path, not only a basename",
          cited(spelled("skills/gws-shared/SKILL.md", 73)),
          [("skills/gws-shared/SKILL.md", 73, 73)])
    check("and a dependency's path is a citation too",
          cited(spelled("google/genai/_api_client.py", 656, 681)),
          [("google/genai/_api_client.py", 656, 681)])
    # Read once and whole. A path that also matched from its last segment
    # would be counted twice and resolved twice, and the second reading of it
    # is a bare `model.py` this repo does not have.
    check("a path is one citation and not two",
          len(cited(spelled("_gaos/types/interactions/model.py", 53))), 1)

    # Three sentences a sweep has to walk past. The first is what a citation
    # is written inside on this tree, the other two are what a looser pattern
    # would drag in from prose that is not pointing anywhere.
    check("backticks and parentheses around one do not hide it",
          cited("the guard (`gws.py" + ":830`) declines"),
          [("gws.py", 830, 830)])
    check("a host and a port is not a citation", cited("raspberrypi.local:8000"), [])
    check("nor is a time of day", cited("the 07:30 slot"), [])
    check("nor is a suffix inside a longer word",
          cited("the copy.pyc" + ":4 artefact"), [])
    check("nor a version", cited("playwright 1.62.0"), [])

    check("a line that is only punctuation says nothing",
          [says_nothing(line) for line in
           ('', '   ', '"""', '    )', '  */', '|  |', '# ')],
          [True] * 7)
    check("and one with a word in it says something",
          [says_nothing(line) for line in
           ('REFRESH_S = 600.0', '    return found', '# The scratchpad.')],
          [False] * 3)

    # Which side of the line each rule lands on. `reach_cases` exists because
    # a sweep that stopped reaching the tree would be green and mean nothing;
    # this is the same worry one rule down. The drift finding is not a case,
    # so nothing else in this file would notice if the call to `drifted` went
    # away — and a citation running off the end of its file must still be a
    # case, not a note.
    with tempfile.TemporaryDirectory() as scratch:
        fixture = Path(scratch) / "sample.py"
        fixture.write_text("FIRST = 1\n\n)\n", encoding="utf-8")
        check("a range that ends on a brace is drift and not a failure",
              [verdict(fixture, 1, 3), bool(drifted(fixture, 1, 3))],
              ["", True])
        check("and one that runs off the end is a failure and not drift",
              [verdict(fixture, 1, 9), bool(drifted(fixture, 1, 1))],
              ["the file stops at line 3", False])
        # Both are cases and not notes for the same reason: `drifted` indexes
        # on either end, so a range that reaches it malformed is a traceback
        # printed underneath an `ok` rather than a verdict.
        check("a line number below one is not in any file",
              verdict(fixture, 0, 0), "line numbers start at 1")
        check("nor is a range that runs backwards",
              verdict(fixture, 3, 1), "the range runs backwards from line 3")


def relocate_cases() -> None:
    print("\n== a range that moved, against a range that went ==")

    # One range, four futures. Written as data because the whole claim of this
    # section is that these four are four and not two: `drifted` answers the
    # same for every one of them, which is why it is checked alongside.
    was = ["def _read_state() -> dict[str, float]:", "    return {}"]
    tail = ["", "REFRESH_S = 600.0", "", "def other() -> None:", "    pass"]
    unmoved = ["# one", "# two"] + was + tail
    later = ["# one", "# two", "# three", "# four"] + was + tail
    earlier = ["# one"] + was + tail
    without = ["# one", "# two", "REFRESH_S = 600.0", "    pass"]

    check("a range still where it was reads as unmoved",
          relocate(was, unmoved, 3), "")
    check("a range pushed down is named at the line it is on now",
          relocate(was, later, 3),
          "moved to 5: 'def _read_state() -> dict[str, float]:'")
    check("and one pulled up the same way",
          relocate(was, earlier, 3),
          "moved to 2: 'def _read_state() -> dict[str, float]:'")
    check("a range whose subject is gone says so instead of a number",
          relocate(was, without, 3),
          "gone from the file: 'def _read_state() -> dict[str, float]:'")

    # The case the whole section exists for. `drifted` cannot separate the two
    # above: both land on a line with a word in it, so both are silent.
    with tempfile.TemporaryDirectory() as scratch:
        for name, lines in (("moved.py", later), ("vanished.py", without)):
            (Path(scratch) / name).write_text(
                "\n".join(lines) + "\n", encoding="utf-8")
        check("the blank-line rule cannot tell those two apart",
              [drifted(Path(scratch) / name, 3, 4)
               for name in ("moved.py", "vanished.py")], ["", ""])

    check("a key too short to be worth searching for is declined",
          relocate(["    pass"], without, 9), f"{UNSURE} at line 9")
    check("and a key in two places is the nearer one, said out loud",
          relocate(["REFRESH_S = 600.0"],
                   ["REFRESH_S = 600.0", "x", "y", "REFRESH_S = 600.0"], 3),
          "moved to 4, 1 of 2 places it reads: 'REFRESH_S = 600.0'")
    check("indentation is not part of what moved",
          relocate(["    def _read_state() -> dict[str, float]:"],
                   unmoved, 3), "")

    # And the plumbing under all of that, in a repository of its own: a real
    # `git blame` and a real `git show`, because `relocate` is only as good as
    # the snapshot handed to it and a `history` that returned `""` forever
    # would leave every case above passing over a pass that never runs.
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        (root / "target.py").write_text(
            "\n".join(was + tail) + "\n", encoding="utf-8")
        (root / "citing.py").write_text(
            "# the state read is " + spelled("target.py", 1, 2) + "\n",
            encoding="utf-8")
        for args in (("init", "-q"), ("add", "-A"),
                     ("-c", "user.name=check", "-c", "user.email=c@localhost",
                      "-c", "commit.gpgsign=false", "commit", "-qm", "one")):
            history(*args, root=root)
        sha = wrote("citing.py", 1, root=root)
        check("history names the commit that wrote a citing line",
              bool(sha) and len(sha) == 40, True, sha[:7] or "no git here")
        # Three lines above the cited range, which is the ordinary edit.
        (root / "target.py").write_text(
            "\n".join(["# a", "# b", "# c"] + was + tail) + "\n",
            encoding="utf-8")
        check("and its copy of the cited file is what the range used to say",
              (snapshot(sha, "target.py", root=root) or [])[:2], was)
        check("so an insertion above a cited range is reported as a move",
              relocate(snapshot(sha, "target.py", root=root)[:2],
                       (root / "target.py").read_text(
                           encoding="utf-8").splitlines(), 1),
              "moved to 4: 'def _read_state() -> dict[str, float]:'")
        check("and no history at all reports nothing rather than lying",
              [wrote("target.py", 1, root=root / "nowhere"),
               snapshot(sha, "target.py", root=root / "nowhere")], ["", None])

    # Pre-#247 fixture: verify fail-before (stale citations in pre-#247 coder skill
    # are caught by relocate) and pass-after (post-#247 coder skill text is clean).
    target_path = ROOT / "tools" / "pi_deploy.py"
    if (WORKSPACE / ".git").exists() and target_path.exists():
        aimed = f"{ROOT.name}/tools/pi_deploy.py"
        then_247 = snapshot("463f9d9", aimed, root=WORKSPACE)
        if then_247 is not None and len(then_247) >= 1204:
            now_lines = target_path.read_text(encoding="utf-8").splitlines()
            check("pre-#247 pi_deploy.py (1171) fails relocate (gone)",
                  relocate(then_247[1170:1171], now_lines, 1171),
                  'gone from the file: \'parser.add_argument("phase", nargs="?", choices=\'')
            check("pre-#247 pi_deploy.py (1197-1204) fails relocate (moved)",
                  relocate(then_247[1196:1204], now_lines, 1197),
                  "moved to 1686: '# writes both of these files two phases earlier:'")
            coder_skill = WORKSPACE / ".claude" / "skills" / "issue-looping-coder" / "SKILL.md"
            if coder_skill.exists():
                skill_text = coder_skill.read_text(encoding="utf-8")
                check("post-#247 coder skill no longer has stale pi_deploy.py:1171",
                      "pi_deploy.py:1171" in skill_text, False)
                check("post-#247 coder skill no longer has stale :1197-1204",
                      ":1197-1204" in skill_text, False)


def reach_cases() -> None:
    print("\n== the tree this is reading ==")

    paths = tree()
    named = index(paths)
    # A sweep that quietly stopped reaching the tree would report every
    # citation in it as held, which is the one way this script can be green
    # and mean nothing. So it says out loud which corners it reached.
    for corner in ("src/ai_terminal/live/main.py",
                   "src/ai_terminal/live/static/js/app.js",
                   "src/ai_terminal/live/static/css/style.css",
                   "src/ai_terminal/live/skills/gws-shared/SKILL.md",
                   "deploy/ai-terminal-kiosk.service",
                   "tools/sources_check.py",
                   "README.md"):
        check(f"{corner} is in the sweep", corner in named, True)

    if (WORKSPACE / ".claude").is_dir():
        for corner in (".claude/skills/issue-looping-coder/SKILL.md",
                       "issue-looping-coder/SKILL.md",
                       ".claude/hooks/role_guard.py",
                       "role_guard.py"):
            check(f"{corner} is in the sweep", corner in named, True)

    check("a vendored spike is not",
          any("spikes" in str(path) for path in paths), False,
          "its README is what would make the root one ambiguous")
    check("and the root README is a single file",
          len(named["README.md"]), 1)
    check("sibling projects are not in the sweep",
          any(p.is_relative_to(WORKSPACE) and not p.is_relative_to(ROOT)
              and not p.is_relative_to(WORKSPACE / ".claude")
              and not p.is_relative_to(WORKSPACE / ".agents") for p in paths), False)

    # And that gitignored state living beside the checkout stays outside it.
    # Given a root of its own rather than probed on this one, because a clean
    # clone has none of those files to find and the case has to hold there
    # too -- the machine this matters on is the one with the monitor running.
    with tempfile.TemporaryDirectory() as scratch:
        beside = Path(scratch)
        (beside / "gws_cache").mkdir()
        (beside / "gws_cache" / "itinerary.md").write_text(
            "x\n", encoding="utf-8")
        (beside / "current_issues.md").write_text("x\n", encoding="utf-8")
        (beside / "custom.toml").write_text("x\n", encoding="utf-8")
        (beside / "README.md").write_text("x\n", encoding="utf-8")
        check("and state beside the checkout is not in the sweep",
              [path.name for path in tree(beside)], ["README.md"])


def citation_cases() -> None:
    print("\n== every citation in the tree, against what it names ==")

    paths = tree()
    named = index(paths)
    packaged = installed()
    outside = 0
    stale: list[str] = []
    walked: list[str] = []
    unreached = 0
    unsure = 0
    # Two citing lines in the same commit usually cite the same file, and a
    # `git show` of `app.js` is 2 000 lines. Read once per pair.
    snapshots: dict[tuple[str, str], list[str] | None] = {}

    for path in paths:
        where = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path.relative_to(WORKSPACE)
        where_repo = path.relative_to(GIT_ROOT)
        for line_no, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            for name, first, last in cited(line):
                at = f"{where}:{line_no} cites {spelled(name, first, last)}"
                clean_name = re.sub(r"^(?:\./|\.\./)+", "", name)
                targets = named.get(clean_name, [])
                if not targets and name in named:
                    targets = named[name]
                unique_targets: list[Path] = []
                seen_targets: set[Path] = set()
                for t in targets:
                    res = t.resolve()
                    if res not in seen_targets:
                        seen_targets.add(res)
                        unique_targets.append(t)
                targets = unique_targets

                # Every branch says why in `got`, not in `detail`: `check`
                # prints the detail when there is one, and a failure has to
                # arrive with its reason on the same line as its name.
                if len(targets) > 1:
                    where_else = ", ".join(
                        str(t.relative_to(ROOT) if t.is_relative_to(ROOT) else t.relative_to(WORKSPACE))
                        for t in targets)
                    check(at, f"{len(targets)} files answer: {where_else}",
                          "one file answers to that name")
                    continue
                if not targets:
                    # Somebody else's file. All this can ask of it is that the
                    # name is one the environment has, which is what catches a
                    # rename in `src/` arriving here as a dependency.
                    outside += 1
                    seen = Path(name).name in packaged
                    check(at, "outside this repo, "
                          + ("installed" if seen else "and nowhere installed"),
                          "outside this repo, installed")
                    continue
                held = verdict(targets[0], first, last)
                check(at, held or "is inside the file", "is inside the file")
                if held:
                    continue
                if why := drifted(targets[0], first, last):
                    stale.append(f"{at}: {why}")
                # And the same range against what it said when this sentence
                # was written. Untracked, uncommitted or exported: no history
                # to read, counted rather than passed over.
                sha = wrote(str(where_repo), line_no, root=GIT_ROOT)
                aimed = str(targets[0].relative_to(GIT_ROOT))
                if not sha:
                    unreached += 1
                    continue
                if (sha, aimed) not in snapshots:
                    snapshots[(sha, aimed)] = snapshot(sha, aimed, root=GIT_ROOT)
                then = snapshots[(sha, aimed)]
                if then is None or last > len(then):
                    unreached += 1
                    continue
                why = relocate(then[first - 1:last],
                               targets[0].read_text(
                                   encoding="utf-8").splitlines(), first)
                if why.startswith(UNSURE):
                    unsure += 1
                elif why:
                    walked.append(f"{at}: {why}")

    print(f"\n   {outside} of them point outside this repo")
    # Underneath the count and not among the cases, for the reason at the top
    # of this file. Named as what it is: nothing here is known to be wrong,
    # only known to be pointing somewhere nobody writes a sentence about.
    if stale:
        print(f"\n   {len(stale)} point at a line that says nothing:")
        for line in stale:
            print(f"     {line}")
    # Said even when it is zero, because the count that matters here is the
    # one nobody looks at: a pass that quietly reached nothing reports the
    # same empty list as a tree with no drift in it.
    print(f"\n   {unreached} could not be re-located: nothing in this "
          "checkout's history says what they were written against, and "
          f"{unsure} had no line distinctive enough to search for")
    if walked:
        print(f"\n   {len(walked)} name a line their subject has left:")
        for line in walked:
            print(f"     {line}")
    # One case over the list rather than one per citation, and after the list
    # is printed. `check` leaves on the first failure, so a case per citation
    # would name one and hide every other — and this report is only worth
    # having whole, because the number beside a moved line is where to start
    # reading and not what to paste in. So the run prints all of them and then
    # says it did not hold.
    check("no citation names a line its subject has left",
          len(walked) <= BASELINE, True,
          f"{len(walked)} have moved since the sentence citing them was written "
          f"(baseline {BASELINE})")


def main() -> None:
    shape_cases()
    relocate_cases()
    reach_cases()
    citation_cases()
    print(f"\nall {RAN} cases hold")


if __name__ == "__main__":
    main()
