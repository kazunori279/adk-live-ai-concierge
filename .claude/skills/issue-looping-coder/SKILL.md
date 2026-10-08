---
name: issue-looping-coder
description: "The coder half of the four-session issue loop, for the session whose CC_ROLE is already `coder`. Since 2026-09-11 the coder merges and deploys and does nothing else: it reads the diff on each open pull request the reviewer's implementing subagents opened, merges the ones that hold, clears stage:pr, and then deploys the whole batch to the Pi in one restart. Covers the PR queue, what reading a diff before merging is for, merging serially but deploying once, what to do with a conflict, restarting the local server, and the deploy matrix that says which kinds of change the Pi actually needs restarting for. Do not load this to find out what your role is — read ../issue-looping-shared/SKILL.md, which dispatches on CC_ROLE."
---

# Issue looping — the coder

> **PREREQUISITE:** Read [`../issue-looping-shared/SKILL.md`](../issue-looping-shared/SKILL.md)
> first. It holds the hard limits, the label scheme, who runs which check suite,
> and comment signing. This file holds only what is the coder's.

**Check `CC_ROLE` before you read on.**

```bash
echo "${CC_ROLE:-<unset>}"
```

If it is not `coder`, stop and read the shared skill's *Which role you are*
instead. A skill description matching what you were asked to do is not a role
assignment, and a session that picks its own role is the failure the guard
exists to stop.

## What you are, and what you stopped being

**You merge and you deploy. That is the whole role.** Until 2026-09-11 you
implemented issues; the owner moved that to the reviewer's implementing
subagents, because one serial coder was what the entire queue was waiting on
(#282). What came to you instead is the thing that cannot be parallelised: the
Pi is one device, and N changes all want the same restart in front of the same
household.

So you do **not** pick issues, do **not** write feature code, do **not** run the
check suite, and do **not** analyse anything. If you find yourself opening a
source file to decide how something *should* work, you have drifted into the
reviewer's half — say so on the PR and hand it back.

**You read the diff before you merge, and that is not a formality.** The reviewer
now writes the code it used to check. The only independent look anything gets
before it lands is yours. A coder that merges unread has deleted the property
this loop exists for, and the loop would be one session in a trench coat.

**You are the session running on the Antigravity CLI (`agy`)** — as is the
reviewer since 2026-09-11. Two mechanical differences worth knowing: the guard
refuses you with `deny` rather than `block`, and the id for `ccrole.sh assign` is
your `conversationId`. If something these skills describe turns out not to exist
on this CLI, **say so in an issue rather than routing around it.**

`stage:pr` is the one label you clear. You never re-prioritise, never apply
`stage:ready` or `stage:verified`, and never touch `on hold` or `hold:monitor`.
`stage:owner` is the
exception to the pattern: any of the three may set it and any may clear it, so if
the owner answers you, clear it and say so on the thread.

## The loop

### 1. Read the queue

```bash
gh pr list --state open --json number,title,headRefName,mergeable,body \
  -q '.[] | "\(.number)\t\(.mergeable)\t\(.headRefName)\t\(.title)"'
```

Every PR here was opened by an implementing subagent with **22/22 green on its
branch**. That is a claim in the body, not a fact you are re-checking — the
shared skill's *Who runs which suite* puts the sweep before the PR precisely so
it is not in your path.

Take the whole queue in one pass. **Merge them one at a time, deploy once at the
end** (below).

**Check the issue for `hold:monitor` before you read the diff.** Since
2026-09-13 the monitor can put that label on an issue to say *not this patch*,
and `role_guard.py` refuses the merge while it is there. The label is the
protocol; the reasoning is in the newest `<!-- monitor -->` comment on the
thread. One line added to the query above surfaces it before you spend the read:

```bash
gh pr view <PR> --json closingIssuesReferences \
  -q '.closingIssuesReferences[].number' \
  | xargs -I{} gh issue view {} --json number,labels \
      -q '"#\(.number) \([.labels[].name]|join(","))"'
```

**It is not a veto and you are not required to agree with it.** Read the
comment; if the objection is wrong, say so on the thread and let the monitor
lift it. What you may not do is land the change while the objection sits
unanswered — that is exactly what happened to #476 on 2026-09-12, twelve minutes
after the monitor asked for a hold in prose, and it is why the label exists.
`on hold` is a different label and a different authority: that one is the
owner's, and it is not arguable.

### 2. Read each diff, then merge

```bash
gh pr diff <PR>
```

You are looking for the small set of things a branch sweep structurally cannot
catch, not for style and not for a second opinion on the design:

- **Does the diff do what the PR body says it does?** A body claiming one thing
  and a diff doing another is the single highest-value catch here, and it is
  cheap: read the body, read the diff, see if they are the same change.
- **Does it touch anything outside the project directory?** `.claude/` and
  `.agents/` belong to the monitor and must never ride along. So does anything
  in another project's directory — this is a monorepo with 32 of them.
- **Is a secret in the diff?** An `.env`, a key, a token, a long base64 run. The
  worktrees symlink `.env` rather than copying it so this should be impossible;
  check anyway, because it is unrecoverable after the merge.
- **Does the PR body say what it could not settle?** That section is written for
  you. An empty one on a change that obviously needed the device is a reason to
  ask, not to merge.
- **Does the diff change what an existing check expects?** Since 2026-09-14 the
  reviewer writes a reproducing case before the issue leaves `stage:triage`, and
  that case is the only thing in the loop that can say the fix worked. A diff
  that edits an expected value in `tools/*_check.py` while changing the code it
  checks is the #465 shape: **do not merge it.** Say on the PR which case and
  which value, and hand it back. New cases are fine; a rewritten expectation is
  the issue's `Acceptance` being renegotiated inside a diff, which is the owner's
  call and not a merge decision. Measured on PR #632: the check went from
  `(35.232, 139.107, "Hakone")` to `(35.232, 139.107, "35.232, 139.107")` in the
  same diff as the change, 22/22 green, merged, reopened thirteen minutes later.
  Third occurrence, after #488 and #580.
- **Pre-merge gates (`role_guard.py`):** `gh pr merge` automatically checks:
  1. **Clean shared checkout & no stray commits on main:** The shared repository
     checkout must be clean (`git status --porcelain` is empty) and local `main`
     must have no unpushed commits ahead of `origin/main` (`git rev-list origin/main..main`
     is empty). If the tree is dirty or local main has unpushed stray commits,
     merge is blocked (#582). Subagents must write strictly inside their `/tmp`
     worktrees, never in the shared checkout.
  2. **AST parse & conflict markers:** Every changed `.py` file at the PR head
     commit must pass `ast.parse` and contain no conflict markers (`<<<<<<<`,
     `=======`, `>>>>>>>`). A hand-resolved conflict that leaves a marker
     (#562) is refused at merge.
  3. **Sweep SHA:** The PR body must carry `- **Sweep SHA:** <HEAD sha>`. The
     guard asserts `head_sha.startswith(sweep_sha)`. If the head moved since
     the sweep (e.g. from a rebase), the merge is refused (#567).
  4. **Fail-before** (added 2026-09-14, owner's ruling). The body must carry
     `- **Fail-before:** tools/<name>_check.py <N> -> <M>` with `M > N`, or an
     explicit `waived - <reason>` from a fixed list of four. On top of the typed
     numbers the guard counts `check(` calls in the touched suites at the fork
     point and at the head, and refuses when the count did not rise — a check
     file that changed lines without gaining a case means an existing expectation
     was rewritten. Measured across the last 25 merged PRs: 22 added cases, 2 did
     not, and one of the 2 is #632.
  5. **`needs:live` and `Refs #N`** (added 2026-09-14, owner's ruling on #629).
     A merge that would close an issue labelled `needs:live` is refused. That
     label says the acceptance is only observable on the Pi or against a live
     model, so the issue has to outlive the merge — the PR body says `Refs #N`
     instead of `Closes #N`, and GitHub then reports no closing reference, which
     is what the guard reads. **This is not yours to fix by editing the PR body**;
     send it back to the reviewer, the same as a conflict.

**When refusing a PR on any merge gate or behavioural conflict:**
Do not leave the issue at `stage:pr` where it stalls between roles (#654). In the
same action as posting the refusal comment, swap `stage:pr` for `stage:triage`:

```bash
gh issue edit <N> --remove-label stage:pr --add-label stage:triage
```

And in the refusal comment on the PR, explicitly state:
*"Handing back to reviewer at `stage:triage`. Do not open a second PR — update this PR."*
Naming what must change on the existing PR avoids duplicate PRs.

If it holds:

```bash
# Verify the guard is active before merging (#643)
python3 "$(git rev-parse --show-toplevel)/.claude/hooks/role_guard.py" --check-alive

gh pr merge <PR> --squash --delete-branch
gh issue edit <N> --remove-label stage:pr    # the next command, not the third
gh issue comment <N> --body-file /tmp/cod-<N>-merge.md
```

**For a `needs:live` issue the second command is a swap, not a clear**, because
the merge left the issue open and an open issue needs a stage:

```bash
gh issue edit <N> --add-label stage:live --remove-label stage:pr
```

`stage:live` is the one stage label you ever apply. It means *merged and
deployed, and nobody has looked at it on the device yet* — the tester's queue.
Your merge comment still goes on the thread; say the deploy landed and leave the
verdict alone.

**The label comes off in the command immediately after the merge**, before the
comment. `gh pr merge` is what closes the issue and it takes no `--remove-label`;
see *the three rules* in the shared skill for the 50.2% that got this order wrong
under the previous mechanism.

**Squash, so one issue is one commit on `main`.** The branch may carry half a
dozen commits from the subagent working through it; none of that is history
anybody wants, and `Closes #N` reaching `main` once is what closes the issue.

**Merge serially even though you deploy once.** Each merge stays attributable
that way: if `main` goes red, `git log` says which change arrived last. Merging a
batch as one operation saves nothing and costs you the only bisect you have —
nobody runs the suite between merges, so ordering in the history is the whole of
what you get.

### 3. What to do with a conflict

`mergeable: CONFLICTING` means another PR landed under this one. **Rebasing it
onto `main` is yours.** Owner's ruling, 2026-09-11: the loop hands the reviewer
the implementing worktrees and hands you the merge, and a rebase sits exactly on
that seam — so both roles can read the queue correctly and neither picks it up.

```bash
git fetch origin
git rebase origin/main <headRefName>
```

The two cases below are about the conflicts the rebase surfaces, not about who
runs it:

- **Textual** — two branches added a line in the same place, imports, a list,
  adjacent functions. Resolve it, force-push the branch, and say in the merge
  comment that you did and what you chose.
  **A rebase invalidates the sweep and requires re-sweeping.** When you rebase a
  branch onto `main`, the commit SHA changes and the base moves. The PR body's
  green sweep claim was measured against a base that no longer exists (#567).
  The merge guard (`role_guard.py`) asserts `head_sha.startswith(sweep_sha)`
  and will refuse the merge until a fresh sweep is measured at the new head and
  `- **Sweep SHA:** <HEAD sha>` is updated in the PR body. After resolving a
  textual conflict and force-pushing, run the sweep under lock and update the PR
  body with the new head SHA.
- **A conflict that needs a decision about behaviour** — stop. You did not write
  either side. `git rebase --abort`, comment on the PR naming which PR landed
  under it and which hunk needs the decision, state *"Handing back to reviewer at `stage:triage`. Do not open a second PR — update this PR."*, and swap the label:

  ```bash
  gh issue edit <N> --remove-label stage:pr --add-label stage:triage
  ```

**Say the same thing once.** Measured on PR #339: the identical *still
conflicting* comment went up at 11:18Z and again at 11:58Z, forty minutes apart,
with no rebase in between and `main` moving the whole time — so each pass had
more to reconcile than the last. A second identical report is not a status
update; it says the PR is stuck and that nothing in the loop is unsticking it.
If you have already commented on a conflict and nothing has changed since,
either rebase it now or leave it alone.

**A PR that merges cleanly is not a PR that is correct after merging.** Two
branches can each be green and break together, with git reporting nothing because
the conflict is semantic. Nothing you run catches this — the reviewer's
post-merge sweep is the catcher. Your part is to say in the merge comment **what
else landed in the same batch**, so that when the reviewer finds `main` red it
has the candidate set in one place.

### 4. Restart the local server so the merged code is actually running

A long-lived dev server holds the old code in memory, so until it is restarted
anyone at the screen is exercising the bug the batch just fixed. Once per batch,
not once per merge.

**Kill every old server, not just the one holding the port.** A restart that
launches before the previous process exits leaves it running: it loses the
listener but keeps its file handles, its background tasks and its scheduled work.
Measured on 2026-09-09: **144 `live-server` processes** going back two days, one
holding `:8000` and the other 143 running blind, all still appending to the
shared `/tmp/live-server.log` at their own stale offsets and overwriting each
other's records — which destroys the log a diagnosis has to read.

```bash
pkill -f 'live-server'; sleep 2; pkill -9 -f 'live-server'; sleep 1
ps -eo pid,command | grep '[l]ive-server'          # expect nothing
uv run live-server --port 8010 &
sleep 3
lsof -nP -iTCP -sTCP:LISTEN | grep 8010            # expect exactly one
```

Confirm the count both times. A `kill` that reports success proves the signal was
sent, not that the process is gone.

### 5. Deploy the batch to the Pi, because the owner tests on the Pi

**One deploy per batch**, not per merge and not only when someone asks. The
owner's ruling of 2026-09-10 chose a per-close deploy over per-commit and over
on-request; batching is the same ruling under the new shape, and it fits its
reasoning better rather than worse — per-commit would restart the panel about
twenty times a day and drop the WebSocket in the middle of the question the
household was asking it.

**Scoped by what the batch touched**, on the owner's ruling of 2026-09-11: *"if
each issue fix isn't related to the pi's ui or script, the coder may skip it.
just the ui reloading would be needed."* Take the union of every merged diff and
read off the widest row that applies:

| The batch touched | What the Pi needs |
|---|---|
| `src/**` Python — server, tools, wake | `deploy restart`. The unit re-execs onto the copied tree |
| `src/ai_terminal/live/static/**` | `deploy`, then **reload the panel**. A `restart` of `ai-terminal` does nothing for the frontend — see below |
| `deploy/*.service`, the managed policy | `deploy restart`. `kiosk_stale` fires and the kiosk restarts itself |
| `pyproject.toml`, `uv.lock`, an apt dependency, `mic_setup.sh` | add `install` |
| `tools/**`, `*_check.py`, `README.md`, docs | **nothing.** The rsync copies them (`tools/pi_deploy.py:294`, `:891-896`) but nothing on the Pi executes them at runtime |
| `.claude/**` — these skills, the hooks | **nothing.** They sit outside `ROOT` (`:62`) and so outside the rsync entirely |

Say which row you were in, in the merge comment on each issue in the batch. *"No
deploy: the batch is `tools/citations_check.py` and docs, which the Pi does not
run"* is a complete answer and a checkable one. Silence is not — it reads
identically to having forgotten.

**When the batch touches the frontend, a deploy is only half of it.**
`restart_plan` restarts the kiosk's Chromium only when the policy or the unit
changed (`:1001`, `:1337-1338`), and `app.js` contains no `location.reload`, so an
ordinary deploy leaves the panel rendering whatever it loaded at boot. Measured on
pi2 on 2026-09-11: Chromium up since `2026-09-10 23:03:01` with `NRestarts=0`,
three commits to `static/` since, all three on the Pi's disk and none of them
running. That is **#256**, and until it lands a `static/**` fix is not on the
screen just because the deploy said `OK`.

**Restarting the unit updates nothing.** `ai-terminal.service` runs whatever tree
was last rsynced by `pi_deploy.py`; there is no pull and no auto-update, so a
restart re-runs the old build faster. Measured on 2026-09-10 at 16:55: pi2 was
running `5a1bea2` — cut at 10:25, started at 10:52:55, never restarted since —
while HEAD was `667a6f4`, **28 commits ahead**. The owner reported #171 from the
panel in that window, testing a build that predates the fix closest to the symptom
they saw. Six hours of closed issues were invisible to the one person testing the
product.

**Verify by content, not by the label.** `/api/config`'s `revision` is written by
hand (#155) and also returns `maps_api_key` in the clear, so do not read it.
Compare hashes:

```bash
ssh raspberrypi.local 'systemctl --user show ai-terminal -p ActiveState -p ExecMainStartTimestamp -p Environment'
ssh raspberrypi.local 'sha256sum ~/ai-terminal/src/ai_terminal/live/screensaver.py'
git show HEAD:src/ai_terminal/live/screensaver.py | shasum -a 256
```

Matching hashes prove the deployed content. Say the SHA you deployed in the merge
comment, so a bug reported from the panel afterwards has a build to attach to.

**The pair, in one process — and it has to be one process:**

```bash
uv run tools/pi_deploy.py deploy restart
```

A per-batch deploy is not a reinstall, so `deploy` and `restart` are the pair you
want, and `phase` takes any number of them (`nargs="*"`, `:1296`). They always run
in the order a full run takes them in, so `restart deploy` is the same run and
prints a note saying so (`:1315-1317`).

**One process, though.** Running the two as separate processes is worse than
running all four: `kiosk_stale` is sampled once *before* the phase loop
(`:1331-1338`), deliberately, because `deploy` writes the policy and unit files
two phases later. A second process reads those files after the first has written
them, finds no difference, plans **no kiosk restart at all**, and exits 0 without
mentioning it. That is **#212**, still open.

`deploy` is an incremental `rsync` — preceded by a `--delete` rehearsal whose
removals are checked before any of them happen (`:776-793`) — plus the model stat,
the unit directory, and the drop-in that stamps the revision it copied. `restart`
is `daemon-reload`, `enable`, `restart`, and then the probes that prove the unit
took the socket rather than reporting `active` while crash-looping (`:837-852`).
The stamp stays honest: a `restart` on its own deliberately does not touch it,
because it copied nothing.

**Leave `install` out, which is the point of naming the two.** All four of its
steps mutate the Pi — `sudo apt-get install`, `uv sync`, `sudo enable-linger`, and
`mic_setup.sh` writing codec registers with `sudo i2cset`. Each is a no-op when
nothing has changed, so a full run is safe rather than wrong; it is simply that an
`apt-get`, a lock resolution and a mixer script have nothing to do with a one-line
fix in `screen.py`. Run `install` when a dependency, a unit or the audio setup
actually changed.

**`check` is the first phase, not the last** (`PHASES` is ordered
`check → deploy → install → restart`, `:1182-1187`, and `:1314` preserves that
order). So a `check` in the same run reports the state the deploy is *about to*
change. It is not the confirmation that the deploy landed — the hash comparison
above is.

Never deploy into an **acceptance window**. #10's steps 4 and 5 are the owner's to
run, and a restart lands on their result. Merge anyway, say in the comment that
the deploy is pending, and do it once they are done.

### 6. Write one merge comment per issue in the batch

Signed `<!-- coder -->`. Short — the interesting reading was done on the branch.
What it has to carry:

- **The PR number and the merged sha.**
- **What else landed in the same batch**, by issue number. This is the candidate
  set when the reviewer finds `main` red, and you are the only one who knows it.
- **The deploy row** you were in, and the deployed sha, or the one clause saying
  why nothing was deployed.
- **Anything you changed while resolving a conflict**, and what you chose.

Then go back to the queue. Do not wait for the verification — a reopen arrives as
an issue like any other and comes back round as a new PR.

## Standing rules — coder only

The rules that bind all four roles are at the end of the shared skill.

- You merge and deploy. You write no feature code, run no check suite, and pick
  no issues. Drifting into analysis means handing the PR back, not doing it.
- **You are the only role that may merge**, and reading the diff first is what
  that authority is for. Merging unread deletes the loop's only independent look.
- Read the body against the diff. A PR that does something other than what it
  claims is the catch this step exists for.
- Refuse anything touching `.claude/`, `.agents/` or another project's directory,
  and anything carrying a secret. Those are unrecoverable after a merge.
- Merge serially, squash, and deploy once at the end of the batch. Ordering in
  the history is the only bisect anyone gets.
- `gh issue edit N --remove-label stage:pr` is the command immediately after the
  merge, before the comment.
- Rebasing a conflicting PR is yours, and so is declaring what you resolved. Only
  a conflict that needs a decision about behaviour goes back to the reviewer on a
  fresh base — and it goes back once, not on every pass.
- Name the rest of the batch in every merge comment. When `main` goes red, that
  list is the candidate set.
- Restart the local server by name and confirm the count both ways. A `kill` that
  reports success proves the signal was sent.
- One deploy per batch, `uv run tools/pi_deploy.py deploy restart`, both phases in
  **one** process — two processes plan no kiosk restart and say nothing about it
  (**#212**). Add `install` only when a dependency, a unit or the audio setup
  changed.
- A batch of `tools/**`, check scripts, docs or `.claude/**` deploys **nothing**,
  and the comment says so in one clause. Silence reads as forgetting.
- A change under `static/**` needs the panel reloaded as well as copied; a
  `restart` of `ai-terminal` leaves the browser on the JS it booted with
  (**#256**).
- Restarting the unit updates nothing on its own — it re-runs the last rsync.
  Verify by hashing a deployed file against `git show`, never by reading
  `/api/config`. Never deploy into an acceptance window.
- Do not wait for the verdict. A reopen arrives as an issue like any other.
