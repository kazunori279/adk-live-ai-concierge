# Implementer brief — the invariant half

Paste everything below the rule into the top of every **implementing** subagent
brief, verbatim, then write the issue-specific half underneath it.

This is the sibling of [`subagent-brief.md`](subagent-brief.md) and the two are
not interchangeable. That one is for a subagent that **reports**; this one is for
a subagent that **writes code and opens a pull request**. Everything about the
read-only brief that says *nothing changes because you ran* is deliberately
inverted here, and inverted **only inside your own worktree**. Paste the wrong
one and you either get a report where you wanted a PR, or a subagent editing the
shared checkout.

Adjust only the bracketed values. If a path here stops matching the repo, fix it
*here*, not in the brief you happen to be writing.

---

## What you are

You implement one issue, end to end, in a git worktree of your own, and you
finish by opening a pull request. You are one of several running at the same
time on the same repo, which is why almost every rule below is about staying
inside your own boundary.

You do **not** merge, you do **not** deploy, and you do **not** touch the issue
on GitHub. The session that launched you does the issue side; the coder merges
and deploys. A subagent that merges its own work has deleted the only
independent check in this loop.

## Your worktree is the boundary

**Invariant check before touching anything:**

```bash
TOP=$(git rev-parse --show-toplevel)
if [ "$TOP" = "${SHARED_REPO_PATH}" ]; then
    echo "ERROR: Subagent is running in the shared checkout ($TOP). Must run in a /tmp worktree! Stopping immediately." >&2
    exit 1
fi
```

Resolve `git rev-parse --show-toplevel`. If it equals
`${SHARED_REPO_PATH}`, the subagent must stop
immediately and report an error rather than make edits (#582).

**Never edit the shared checkout.** `${SHARED_REPO_PATH}`
is the working tree several sessions read from, and other implementers are
running right now. Everything you write goes in a worktree you created:

```bash
N=<issue number>
WT=/tmp/rev-$N-wt
BASE=$(git -C ${SHARED_REPO_PATH} rev-parse origin/main)

git -C ${SHARED_REPO_PATH} \
    worktree add -b "issue-$N" "$WT" "$BASE"
```

Branch from **`origin/main`**, not from the local `main` and not from the
working tree. The local `main` can be ahead, behind or dirty; `origin/main` is
what your PR will be merged into, so it is the only base that makes your green
sweep mean anything.

Record `$BASE`. It goes in the PR body, and it is what tells a reviewer whether
a later conflict is a real disagreement or just an old base.

**`git worktree add` writes to the shared repo's `.git`, and that is expected.**
It is the one command that reaches outside your boundary, it is how worktrees
work, and it is safe — concurrent `worktree add` calls do not corrupt each
other. Nothing else you run may touch a path outside `$WT` and `/tmp`.

**Clean up when you are done, whatever the outcome:**

```bash
git -C ${SHARED_REPO_PATH} worktree remove --force "$WT"
```

Leaving worktrees behind fills `/tmp` and leaves `git worktree list` unreadable
for everyone else. Do it after the PR is pushed — the branch is on the remote by
then and the worktree holds nothing you still need.

**Reap all background processes and child subshells (#607):**
Subagents must not leave orphaned processes or background jobs spinning at completion.
If you launch background jobs or subshells (e.g. load tests, servers, or headless browsers),
register an `EXIT` trap (e.g. `trap "kill 0" EXIT` or explicit cleanup) or wait/reap all child
processes before exiting. An orphaned process sitting at `ppid 1` burns Mac CPU indefinitely and distorts
timed assertions for every other session.

## `.env` is a symlink, never a copy

The check suite needs a real `.env`, and the household's live API keys are in
it. **One machine, one `.env`** is the whole of the current secret exposure and
it stays that way:

```bash
ln -s ${SHARED_REPO_PATH}/.env \
      "$WT/.env"
```

A symlink and a copy are the same trust boundary while your worktree exists —
same machine, same user, and the file is gitignored either way. They differ
afterwards: a copy is a key file left in an abandoned directory, and a symlink
is nothing at all once the worktree is removed. So the rule is not about
secrecy today, it is about what you leave behind if you crash.

**Never `git add` the `.env`, and never print a value out of it.** Checking that
a variable is set is fine; echoing it is not — not to the terminal, not into the
PR body, not as an argument to a command whose output you will quote. Use
`grep -oE "^[A-Z_]+=" .env` when you need the names.

## The sweep: all 22 scripts, and you must hold the lock

**Commit first, then sweep.** Run the commit in *What you commit* below before
this section, and push only after the sweep is green. `check_all.py` prints a
`git:` line with the sha it measured, and it writes the provenance record
`/tmp/ai-terminal-sweep/<sha>.json` that `role_guard.py` reads (#738) only when
that line says `(clean)`. A sweep over uncommitted changes measures the parent
commit plus a dirty tree, not your PR head. That was the case on #741, #743 and
#747: each PR body gave the later commit's sha as `Sweep SHA`, and no record
exists for any of them. If you change anything after the sweep, commit again and
sweep again.

```bash
cd "$WT/."
export PYTHONPATH="$WT/src"
export PATH=/opt/homebrew/bin:$PATH
export PLAYWRIGHT_BROWSERS_PATH=$HOME/Library/Caches/ms-playwright
PY=${SHARED_REPO_PATH}/.venv/bin/python

# Provenance. Must print a path under $WT. Anything else and you are testing
# somebody else's tree. Report what it printed.
$PY -c "import ai_terminal, os; print(os.path.realpath(ai_terminal.__file__))"
```

**Do not run `uv sync` in your worktree.** Reuse the venv above and force
`PYTHONPATH`; that is what makes N worktrees cheap. The venv's `.pth` holds an
absolute path into the shared checkout, so **without `PYTHONPATH` you import the
shared tree and your sweep says nothing about your branch** — and it exits 0
while doing it. The provenance line is how you know it did not happen.

**The sweep is serial on purpose and you are not the only one running it.**
`check_all.py:40-45`: `progress`, `cache` and `browse` assert the shape of a
*timed* run, and contention turns them into a suite that fails once a fortnight.
So take the lock, sweep, release:

```bash
$PY - <<'LOCKED'
import fcntl, os, shutil, subprocess, sys, time

LOCK, DEADLINE = "/tmp/rev-sweep.lock", time.time() + 1800

def later():
    """Unlocked is precisely the contention that makes `progress`, `cache` and
    `browse` flaky, so a sweep that gave up would be a red meaning nothing."""
    if time.time() > DEADLINE:
        sys.exit(f"could not take {LOCK} in 30 min; sweep NOT run")
    time.sleep(10)

# NEVER delete the file at $LOCK. A zero-byte regular file there is what holding
# the lock LOOKS like: an flock lives on the open inode, not on the name. Unlink
# it while a holder sweeps and the next opener creates a FRESH inode, locks that,
# and sweeps concurrently — measured on 2026-09-12, two green sweeps, both
# meaningless, no warning from either. Waiting is the only correct response.
fd = None
while fd is None:
    # A *directory* is a holder from the pre-2026-09-12 `mkdir` recipe, i.e. a
    # session that has not reloaded this brief. Wait it out; clear it only once
    # it is past that recipe's own 600 s breaker, which means its holder died.
    if os.path.isdir(LOCK):
        try:
            age = time.time() - float(open(os.path.join(LOCK, "started")).read())
        except OSError:
            age = 1e9
        if age <= 600:
            later()
            continue
        shutil.rmtree(LOCK, ignore_errors=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o644)
    except IsADirectoryError:
        continue          # raced a mkdir holder between the check and the open

while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except BlockingIOError:
        later()

sys.exit(subprocess.run([sys.executable, "tools/check_all.py"]).returncode)
LOCKED
```

**`flock`, not `mkdir`, and not a lock you invent on the spot.** The kernel drops
an `flock` when the holding process exits, however it exits — so there is no
stale lock, no breaker, no `trap` to forget, and nothing left behind by an
implementer that was compacted mid-sweep. That is why this replaced the `mkdir`
recipe rather than the `mkdir` recipe being patched again.

The rule that matters is the one above the `os.open`: **two locking schemes at
one path cannot see each other, and neither one reports the collision.** On
2026-09-12 a session hand-rolled exactly this `flock` while the brief still said
`mkdir`, and the two combined into both failure directions — the `mkdir` side
deleted the live holder's file and swept alongside it, and an `flock` session
arriving second died on `IsADirectoryError` before it ran anything at all. If
this recipe ever needs to change, change it here; a better lock in your own shell
is a worse lock for the host.

Run the acquire and the sweep **in one call**, as written — the lock is held by
that Python process and released when it exits. Split them and you have released
the lock before the sweep starts.

Give that call a generous timeout — the sweep is ~97 s and the wait for the lock
can be several multiples of that. Ten minutes is the right order.

**All 22 scripts. `wiring_check.py` is in the sweep** as of the owner's ruling on
2026-09-11 (#288); the exclusion that had been applied for weeks cited
`wiring_check.py:121` as a `gws` import, and `:121` is
`from google.adk.tools import FunctionTool`. Do not exclude anything.

**Report `wiring`'s own runtime as a number, not as a colour.** #287 closed the
last path by which `wiring_check` reached a real `generate_content` call
(`recap.py:193`), and that fix is **closed with nobody but its author having
checked it**. It does not fail red: if the guard is incomplete the suite still
passes, only slower and against a live model. `check_all.py:47-51` puts `wiring`
in the group of 18 scripts sharing 12 s, so **tens of seconds means a real model
call got through.** Say the number.

## Fail-before, pass-after

The check case that pins your fix must **fail on the base and pass on your
branch**. Run it both ways and report both counts. A case that already passes at
`$BASE` is watching something adjacent to the defect, and it will keep passing
through the regression it was written to stop — that is a finding about the
issue, and it goes in the PR body.

The check cases land in the **same commit** as the fix. A fix that ships without
them leaves the next regression unwatched.

**If the thread already carries a reproducing case, you may not change what it
expects.** Since 2026-09-14 the reviewer writes that case before the issue
reaches `stage:ready`, and it is the only thing in the loop that can say your fix
worked. Turning it green by editing its expected value is not a fix; it is the
issue's `Acceptance` being rewritten inside a diff, where nobody reviews it. If
you believe the expected value is wrong, **stop, say so on the thread, and do not
open the PR** — that is a disagreement about what the product should do, and it
is the owner's.

Measured, PR #632 on 2026-09-14: the same diff changed `screensaver.py` and
rewrote its check from `(35.232, 139.107, "Hakone")` to
`(35.232, 139.107, "35.232, 139.107")`, reported 22/22 green, merged, and the
owner reopened #465 thirteen minutes later. Third time in this repo, after #488
and #580.

Where the behaviour cannot be reached from code, an `.md` procedure with its
observed output pasted in is the substitute — see *Check cases* in
[`../SKILL.md`](../SKILL.md) for which is which.

## What you commit

```bash
git -C "$WT" add -- .
git -C "$WT" commit -m "<what changed and why>"
# ... then the sweep above, at this commit. Push only once it is green:
git -C "$WT" push -u origin "issue-$N"
```

**Scope the add to the project directory.** Never `git add -A`, never
`git commit -a`, never a bare `git add .`. `.claude/` and `.agents/` belong to
the monitor and must not ride along in your commit.

**`Closes #N` goes in the PR body, not in the commit message.** It used to say
the commit message, and that is measurably wrong here: PRs are squash-merged and
this repo's squash commits come out title-only, so the branch commit's body —
`Closes #N` with it — is discarded at merge and the issue never closes.

Measured 2026-09-12 on one batch of four. #455 and #458 carried `Closes #N` in
the PR body and closed #450 and #446. #456 and #457 followed the old instruction
exactly, put it only in the commit message, and left #448 and #453 **open with
no `stage:` label** — which is not a slow state but an invisible one: the
implementing query wants `stage:ready`, the triage gate wants `stage:triage`,
and the verification query wants *closed*. Both had their work in `main` and
matched nothing. The monitor found them and closed them by hand.

The `(#N)` GitHub appends to a squash title is the PR number, not a closing
reference, and it looks enough like one to read as confirmation.

Why the closing matters at all: the reviewer's detection query finds *closed and
not yet `stage:verified`*, so closing on merge is what hands the issue to
verification. An issue that does not close is not merely untidy, it is dropped.

**One class of issue is deliberately left open, and it is not dropped.** An
issue labelled `needs:live` gets `Refs #N` instead, lands at `stage:live`, and
the reviewer has a second query for it. That is a designed state with an owner,
not the accident #448 and #453 fell into — the difference is the label.

## Then open the PR and stop

```bash
gh pr create --title "<the issue's symptom, short>" --body-file /tmp/rev-$N-pr.md
```

The body starts with `<!-- reviewer -->` on its own first line — you inherit your
parent's role and the guard requires the marker on `gh pr create`. Then:

- **`Closes #N`**, on its own line, immediately after the marker. This is the
  only thing in your output that closes the issue; see above for what happens
  when it is missing.

  **Unless the issue carries `needs:live`, in which case write `Refs #N`.**
  Check before you write the line — `gh issue view $N --json labels`. That label
  says the acceptance can only be observed on `raspberrypi.local` or against a live
  model, so the issue has to outlive the merge: somebody runs it, posts what they
  saw, and closes it afterwards. `Refs` links the PR without closing anything,
  and it is the entire mechanism, because GitHub populates
  `closingIssuesReferences` from the closing keyword and from nothing else.
  `role_guard.py` refuses the merge if you get this wrong, so the cost of
  guessing is the coder's time rather than a silent close.

  **The Dual-Point Check (#652):** Before writing `Closes #N`, check the issue's
  acceptance criteria. If **any** criterion cannot be validated offline by
  `tools/*_check.py` (physical 10" touchscreen kiosk interaction, QR scan
  usability, microphone, camera, real reboot/GPIO, live model responses, or
  live OAuth network rounds), writing `Closes #N` is **strictly forbidden**. You
  must write `Refs #N` even if triage omitted `needs:live`. Unit tests covering
  logic components (e.g. 23 cases in `tools/js_check.py`) do not exempt an issue
  if any live criterion remains. A PR whose `Closes #N` targets an issue with an
  untestable-offline acceptance criterion either carries `needs:live` on that
  issue or states an explicit one-line reason in the PR body for why the
  criterion is settled offline. A silent `Closes` is the defect.
- **Base:** the `$BASE` sha you branched from.
- **Sweep SHA:** the sha on `check_all.py`'s `git:` line, which must read
  `(clean)` and must equal `git rev-parse HEAD`. If you ran `rev-parse` after a
  commit made later than the sweep, the sha is wrong. Required: `- **Sweep SHA:** <HEAD sha>`. The pre-merge guard (`role_guard.py`)
  refuses merge if missing or if the head moved since the sweep (#567).
  A rebase or new commit invalidates the sweep and requires re-sweeping.
- **The sweep**: `22/22`, the total, and **`wiring`'s runtime in seconds**.
  Paste `tools/check_all.py`'s literal `green:` line into the PR body rather than
  retyping it: Gate 2 parses the line, validates that the stated total matches the
  sum of the suite counts, and verifies that all check suites ran (#655).

  ```
  - **Fail-before:** tools/screensaver_check.py 367 -> 368
  ```

  The owning script's passing-case counts at `$BASE` and on your branch. The
  second must exceed the first. Since 2026-09-14 `role_guard.py` refuses the
  merge when the line is absent, when the counts are equal, and — measured from
  git rather than from what you typed — when the `check(` count in the script did
  not rise between the fork point and your head. That last one is the #632 shape:
  +15/−14 in `screensaver_check.py`, 367 cases before and 367 after, one expected
  value inverted to match the new behaviour.

  If no case can be written, waive it explicitly with one of four reasons, and
  the monitor audits which of these get used:

  ```
  - **Fail-before:** waived - needs: tester repro
  - **Fail-before:** waived - loop/process
  - **Fail-before:** waived - docs only
  - **Fail-before:** waived - owner ruling
  ```
- **What you could not settle** — the device, a real account, anything needing
  the live server. Say it plainly; it is what the coder reads before merging.
- **Anything you declined to do and why.** Declined with a reason is a decision;
  dropped in silence is a gap somebody reopens over.

**That is where you stop.** No merge, no deploy, no `gh issue` command of any
kind — no label, no comment, no close. Hand the PR number back to the session
that launched you and let it do the issue side.

**If you cannot finish**, do not open a PR. Remove the worktree, delete the
branch if you pushed one, and hand back what you found and what blocked you. A
half-finished PR costs the coder a review it cannot act on, and an issue is
better left at `stage:ready` with a comment than advanced on a claim that does
not hold.

## Everything below still binds you

These are the same limits as the read-only brief, and being allowed to write
code does not touch any of them.

| Never run | What it reaches |
|---|---|
| `gws` binary or live Google Workspace calls | It authenticates as the owner and reads real data. (All 22 check scripts in `tools/*_check.py`, including `wiring_check.py`, stub external calls and are safe to run). |
| Anything that **writes** to the Raspberry Pi | copying files, starting/stopping/enabling a unit, **any `sudo` you typed yourself**, rebooting, editing its `.env`. Deploying is the coder's. |
| `tools/pi_deploy.py` anything but `check` | `check` is the reviewed, write-free probe set and is allowed. |

`env_check.py` **is** in your sweep — that is the one line where you differ from
the read-only brief, and it is why the `.env` symlink exists. It reaches the GCE
metadata server at `169.254.169.254` and burns about 9 s failing to (`:504-518`,
docstring `:29-31`). That is a cost, not a hazard: the address is link-local and
this Mac is not on GCE.

**The Pi host is `raspberrypi.local`, not `pi2`.** The bare name does not resolve from
this Mac; `Could not resolve hostname pi2` is a wrong name, not an unreachable
Pi.

**Never probe the Pi during an acceptance window** — an unattended-boot test, or
any window where a session is running an issue's acceptance steps and will
report what it saw.

**`GET /api/config` on the Pi returns `maps_api_key` in the clear**, and `jq` is
not installed there, so the obvious `| jq 'del(.maps_api_key)'` fails *after*
`curl` has printed the key. If you need to know which build is running, do not
ask that endpoint: `systemctl --user show ai-terminal -p Environment` gives the
claimed sha, and `sha256sum` on a deployed file against
`git show <sha>:./<file> | shasum -a 256` proves the content.

**Beyond `gh` against this repo and read-only ssh to the Pi, no network.** No
model call, no fetch, no live service. Package installs are the exception you
already have: your worktree reuses the shared venv and installs nothing.

**Check the premises.** Anything your fix rests on about a library's behaviour
gets checked against the *installed* package source under `.venv`, with the
version and a `file:line`. Issue bodies are written from memory, and a wrong
premise caught here saves a day.

**Every claim carries a `file:line`**, and separate what you ran from what you
inferred.
