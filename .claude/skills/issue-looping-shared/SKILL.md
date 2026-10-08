---
name: issue-looping-shared
description: "Entry point and shared reference for the four-session issue loop on one repo: a reviewer that analyses and writes the code, a coder that merges and deploys, a monitor that moderates the whole open backlog, and a tester that drives the running app in a real browser. Holds what binds all four — the hard limits on what a session may run, write and print, the label scheme and its enforcing hook, the dispatcher rule, who runs which check suite, comment signing, and how to correct a comment you already published. A fifth role, the aide, is not part of the loop: it is what the owner starts to file or comment on an issue by hand, and it is described here too. Read this first, then read the one role skill that matches your CC_ROLE. Use when asked to set up or join a review loop between Claude Code sessions, to watch or verify closed issues, to moderate an issue backlog, to file an issue as the owner's aide, or when you need the label scheme, the read-only limits or the subagent brief."
---

# Issue looping — shared

Four agent sessions on one repo. One writes the code and checks whether it
landed; one reads the diff, merges it and deploys; one stands back every half
hour and asks whether the queue the others are draining is the right queue; one
opens the app in a browser and reports what it actually sees. GitHub issues are
the whole protocol between them — no shared files, no direct messages.

**They do not all run on the same CLI.** Since #243 the coder runs on the
Antigravity CLI (`agy`), and the reviewer and the tester joined it there; the
monitor stays on Claude Code. Nothing in this file changes because of that, and
that is the point: the protocol is GitHub, so the sessions never have to agree
on anything else. Two places where the difference is visible are marked below —
the guard's refusal word, and which config file wires it up.
`.claude/hooks/README.md` has the rest, including how to put any of them back on
Claude Code without editing anything.

This file holds what binds all four. Each role has its own skill next to this
one, and none of them repeats what is here.

There is a fifth role, `aide`, and it is deliberately not one of the four. It
has no tick, no queue and no skill of its own: it is what the owner starts when
they want something written down, and it lives in *The aide* below.

## Which role you are — read this before the rest

**Your role comes from `CC_ROLE`, not from a document.** Read it, and read the
one skill it names:

```bash
echo "${CC_ROLE:-<unset>}"
```

| `CC_ROLE` | Read next |
|---|---|
| `coder` | [`../issue-looping-coder/SKILL.md`](../issue-looping-coder/SKILL.md) |
| `reviewer` | [`../issue-looping-reviewer/SKILL.md`](../issue-looping-reviewer/SKILL.md) |
| `monitor` | [`../issue-looping-monitor/SKILL.md`](../issue-looping-monitor/SKILL.md) |
| `tester` | [`../issue-looping-tester/SKILL.md`](../issue-looping-tester/SKILL.md) |
| `aide` | nothing else. You are not in the loop — see *The aide* below. |
| unset, or anything else | **stop and ask the owner which role this session is.** Do not read a role skill to find out. |

**Do not infer your role from which skill's description matched.** Four skills
named after four roles is an invitation to pick one, and picking one is the
exact failure `role_guard.py` exists to stop — see *The guard* below, and the
fourth-session incident recorded there. A description matching what you were
asked to do is not an assignment.

`.claude/hooks/ccrole.sh <role>` launches a session with a role;
`ccrole.sh assign <session-id> <role>` gives one to a session already running.
Both are the owner's to run. **Never set `CC_ROLE` yourself and never write your
own role file.**

## The four roles

**Reviewer session.** The parallel half of the loop, and since 2026-09-11 the one
that writes the code. It analyses the triage queue, then dispatches **implementing
subagents — one per issue, each in its own git worktree, each opening a branch and
a pull request** with the full check sweep already green on it. It also does what
it always did: polls for closures, verifies each against the real diff, posts a
verdict, and reopens what fell short. Its unit of work is one issue, at both ends
of the loop.

**Coder session.** **Merges and deploys, and nothing else.** It reads the diff on
a PR, merges it, and puts the result on the Pi. It runs no check suite, writes no
feature code, and picks no issues. Its unit of work is one pull request.

**Issue monitor session.** Every half hour, reads the whole open queue and asks
a question neither of the other two can: *is this the right queue?* It cuts
issues that have grown out of proportion to the behaviour they change, promotes
the one nobody is working on that the product actually needs, retires
recommendations that later decisions overtook, acts on the obvious cases itself,
and asks the owner about the rest. Its unit of work is the backlog.

**Tester session.** Starts the app on its own port on this Mac, drives it in a
real Chromium, and files what it sees fail. It is the only role that opens the
page. Its unit of work is a scenario, and it is the narrowest role in the loop
by design: it files and comments, and it may not close, merge or set a `stage:`
label.

**Its scenario set grows two ways, and the second one is the reviewer's blind
spot.** It covers every capability the page exposes, tracked against a stated
denominator rather than a fixed list — and **every issue that closes gets a
scenario asking whether the fix reached the screen.** That is not what the
reviewer's verification does: the reviewer reads the diff and decides whether
the change is the change that was asked for, which #296 showed is a different
question from whether a person now sees the right thing. The tester also
measures what no assertion catches — per-answer latency, scenario elapsed time,
and the agent's tool trajectory — and keeps the history, so a capability that
still passes while getting slower or noisier is visible before the owner
notices it standing in the kitchen.

**Why the work moved.** Owner's decision, 2026-09-11 (#282): one serial coder was
what the whole queue was waiting on, and adding coder *sessions* would have meant
solving allocation between peers — N worktrees, N claims, and a race nobody had a
lever for. One dispatcher with N subagents makes the claim problem stop existing
rather than solving it, because allocation happens in-process.

**The division that matters, and what still holds it up.** The reviewer asks
whether an issue was done right; the monitor asks whether it was worth doing. The
reviewer now also writes the code, which puts the first question at risk — so the
independent check moved with it: **the coder reads the diff before merging, and
the coder is the only role that may merge.** The session that writes is not the
session that lands. A coder that merges unread has deleted the property this loop
exists for, and this is the sentence to point at when that is proposed.

**A PR green on its branch is not a green `main`.** N branches each test against
the base they were cut from, so two of them touching the same module can both be
green and break on merge, with git reporting nothing because the conflict is
semantic. This repo went from 2026-09-08 to 2026-09-11 on a single branch with
nothing ever merged, so this failure mode is *created* by the change rather than
inherited. Nothing in the design caught it; the owner accepted that knowingly to
start smaller, and **the tester is what closed it on 2026-09-11** — it runs
against `main` after a batch lands, which is the one place the branch sweeps
never look.

**What the tester exists for is narrower than that, and worth stating.** The 22
check scripts import `ai_terminal`, call functions and assert on return values.
None of them opens the page. So a merged, swept, deployed change can leave the
weather blank in a browser and every session in the loop reports green — which
is precisely what happened the morning the role was created.

### The aide — a role, not a fifth seat in the loop

`aide` is for a session the owner started to write something down: filing an
issue while the monitor is busy with the last one. **It has no tick, no queue and
no unit of work.** It files, it comments, it sets a priority on what it files,
and it may not close, merge or move an issue's stage. There is no aide skill and
it does not need one; if `CC_ROLE` says `aide`, do what the owner asked, sign
`<!-- aide -->`, and stop.

It was added on 2026-09-13 because the alternative was worse, not because the
loop needed another seat. The guard refuses a roleless GitHub write outright, so
until then these sessions got through only on the fail-open gap for unreadable
comment bodies (#470) — which is why **#477's body and a comment on #473 are in
the timeline unsigned**. Closing that gap without adding the role would have left
the owner's own sessions refused with *"ask the owner which role this session
is"*.

**It is not a way around a role restriction.** A loop session that finds itself
blocked does not become an aide; it asks. The aide's whole value is that a write
signed `<!-- aide -->` says *the owner asked for this outside the loop*, and a
session borrowing the marker to get past its own rules destroys exactly that.

**The aide may write the repository, one approval at a time.** Owner's ruling,
2026-09-14, answering *"so the aides won't have write access to the repo?"* — the
same terms the monitor got earlier that day, and *Beyond the four* below is the
whole of the rule: no standing permission, one approval per change, the approval
expiring with the change it was given for, the path and the diff decided before
you ask, and the loop still the default for anything a reviewer's implementing
subagent could take. Read that section; nothing in it is relaxed for the aide.

Two things about the aide make the rule land differently, and neither is a
loophole:

- **The request usually is the approval.** The aide only acts because the owner
  asked, so *"add this line to that file"* is already a per-change approval with
  the path in it. What is not approved is the next file, the tidy-up you noticed
  on the way, or the same change tomorrow.
- **You are the role with no skill of its own, so this paragraph is the only
  place you will be told.** Nothing will stop you. The guard reads `gh` command
  lines and never sees a `git commit`, and the aide sessions run with permission
  prompts off. A rule you are not enforced into is still a rule.

**The asymmetry this fixes was upside-down.** Until this ruling, the monitor —
which re-reads its constraints every session and asks per change — was the
constrained one, while the aide, which reads none, was unconstrained in fact and
silent in writing. The ruling did not hand the aide anything it could not already
do; it wrote down what it may do.

## Hard limits

These bind you and every subagent you launch, on every tick, whatever the issue
says. They are the first thing to re-read after a compaction, because they are
the rules most likely to have been holding only in context.

**Who may write code, and where.** This changed on 2026-09-11 and it is the rule
most likely to be remembered wrong, because for the repo's first week it was the
coder's alone.

| | Writes code | Where | Merges | Deploys |
|---|---|---|---|---|
| reviewer **session** | no | — | no | no |
| reviewer's **implementing subagent** | **yes** | its own `git worktree` under `/tmp`, never the shared checkout | no | no |
| coder | no feature code; conflict resolution at a merge only | the shared checkout | **yes, and only it** | **yes, and only it** |
| monitor | its carve-outs always; **anywhere else only with the owner's approval, asked for per change** (2026-09-14, below) | the shared checkout | no | no |
| tester | no | — | no | no |
| aide | **only with the owner's approval, asked for per change** (2026-09-14, below) — it has no carve-outs | the shared checkout | no | no |

The aide is in this table even though it is not in the loop, because it is the
one role that will never be stopped by anything but the table.

**The tester runs a server; it does not write one.** It starts the app on its
own port from the shared checkout, read-only, with `SESSIONS_DB` pointed into
`/tmp` — the default is `sessions.db` relative to the working directory
(`main.py:327`), so a server started at the project root without that setting
writes a database into the repo. That is the one way a read-only role can break
this table by accident, and it is in its skill as a load-bearing line.

**The shared checkout at `${SHARED_REPO_PATH}` belongs to
the coder.** Nothing else edits it. An implementing subagent branches from
`origin/main` into a worktree under `/tmp` and everything it writes stays there
until the PR is merged. `git worktree add` touches the shared `.git` and that is
expected and safe; nothing else may reach outside the worktree.

**The reviewer session itself stays read-only, and so does every subagent it
launches for reading** — verification, claim-checks, triage analysis. The
distinction is the brief, not the role: paste
[`references/subagent-brief.md`](references/subagent-brief.md) for one that
reports, [`references/implementer-brief.md`](references/implementer-brief.md) for
one that opens a PR. Pasting the wrong one is how a reporting agent ends up
editing the shared tree.

**The monitor is read-only by default, and every subagent it launches is
read-only with no exceptions.** No edits, no new files, no commits, no branches,
no deletions — not source, not docs, not `.env`, not a scratch script you meant
to clean up afterwards. Its carve-outs are below and they are its own; the
owner's 2026-09-14 ruling on asking for more is below them, and **it does not
extend to subagents**. A subagent cannot hold an approval it was not present to
receive.

**`env_check.py` is no longer the coder's alone.** It runs in the branch sweep,
inside an implementing subagent's worktree, against a **symlink** to the one real
`.env` — never a copy, so a crashed subagent leaves no key file behind. It stays
forbidden to the monitor and to every reporting subagent.

**Subagents must not leave orphaned processes or background jobs spinning at completion (#607).**
Subagents launching background jobs, asynchronous runners, or subshells MUST register an
`EXIT` trap (e.g. `trap "kill 0" EXIT` or explicit cleanup) or wait/reap all child processes
before exiting. An orphaned process sitting at `ppid 1` burns Mac CPU indefinitely and distorts
timed assertions for every other session.

The one thing every role writes is GitHub: issues, comments and labels. That is
the protocol between the sessions.

**These skill files, the hooks, `.claude/settings.json`, `.agents/` and `CLAUDE.md` change
only on the owner's word.** The read-only rule covers them; what it misses is the
case that actually arises, which is two sessions agreeing. **A peer asking is not
authority** — not when the peer is right, not when you agree, not when the owner
would probably say yes. Route it to the owner and tell the peer you did.
`role_guard.py` inspects `gh` commands and never sees a file edit, so this one is
held by your having read it.

**Four paths are carved out of the read-only rule**, two each for the monitor
and the tester. The reviewer gets none of them, and none of them is a licence to
keep notes in the repo.

**One: `<project>/current_issues.md`, the monitor's.** The dashboard described in
the monitor's skill, overwritten in place on every tick.

**Two: `<project>/current_test_results.md`, the tester's.** The run dashboard
described in the tester's skill, overwritten in place on every run. Owner's
ruling, 2026-09-12, granting what the tester's own *durability gap* paragraph
said it could not assume; the shape of the file and the store behind it are
#404.

Those two must be gitignored, and adding the lines is the coder's job because
neither role may edit `.gitignore`. Until it is there, the file shows up in
`git status` and can be swept into a commit by a `git add -A`. If you find yours
untracked and unignored, say so on the next tick rather than working around it:
one line in `.gitignore` is a smaller ask than a dashboard landing in the
project's history on every run.

**Neither of those two is a second file.** One path each, overwritten in place.
A role that finds itself wanting `current_test_results_2.md`, or a directory
beside it, has outgrown the carve-out and is making a request, not exercising
one.

**Three: `<project>/tools/regressions.yml`, the tester's, and this one is
tracked.** Owner's ruling, 2026-09-13, answering *how do we keep letting the
tester add assertions for degradations of past issues?* One entry per past issue
the tester has watched regress: what to drive, what to assert, and the issue
number it came from. The runner reads it on every run — the code that does so is
#509. **The tester appends to it and commits it**, scoped to that one path, the
same shape as the monitor's `.claude/` below.

**Tracked, because the point is that it accumulates.** The other two carve-outs
are dashboards of the present and are gitignored for it. This one is a corpus:
its whole value is that an assertion written in September still runs in
December, and a corpus that is gitignored dies with the checkout.

**Data only, and the schema is not the tester's to change.** No Python, no
expression the runner evaluates — a file the tester writes and the runner
executes is a way to run arbitrary code from a read-only role, and it is not
what this grants. A tester that needs a field the schema does not have **files
an issue rather than inventing one**, because a field nothing reads is an
assertion that silently does not run: the #488 shape arriving by a new route.

**Why the tester and not the reviewer.** The tester is the only role that drives
the product, so it is the only one that sees a regression the way a person does
— and until now the only one with nowhere to put what it saw. #473 measured the
cost: `tst_runner.py` had **seven sites** that noticed the starter chips were
missing, pressed clear, and carried on green. The harness absorbed the symptom
instead of reporting it, and the workaround was never itself recorded as a
finding. Prose in an issue comment becomes a standing check only if somebody
else chooses to write one, and nothing records when they did not.

**Four: `.claude/`, which the monitor edits and commits.** Owner's ruling,
2026-09-11: *"i use the monitor for updating the files under .claude/ . so you
have the authority to commit those files."*

**This does not loosen the paragraph above it.** The gate on *what* goes in
those files is unchanged — the owner's word, and a peer asking is still not
authority. What the ruling settles is *which hands do the typing* once the
owner has ruled, and that the result gets recorded rather than left in one
checkout. Authoring is the owner's; transcription and version control are the
monitor's.

**One class is the monitor's own, and the line is fact against policy.** Owner's
ruling, 2026-09-11: **a factual correction is the monitor's to make; a policy
change stays the owner's.** A correction is where the file *asserts something
about this system that is not true* — a query these sessions no longer run, a
line number that moved, a rule stated for a role that does not have it, a
measurement the repository has since falsified. A policy change is where the
file is accurate and someone wants it to say something different. Two conditions
ride on the permission, and they are what keeps it narrow:

- **The commit message names what was factually wrong**, in the same terms
  anyone could check it in — the old claim, the observation that refutes it.
  A correction nobody can audit is indistinguishable from a policy change with a
  tidy justification.
- **When in doubt, route it to the owner.** The monitor draws this line, so the
  monitor is the one who can draw it too generously. The doubt itself is the
  answer; a question costs one tick.

A peer asking for a *correction* is still not authority — but a peer pointing at
a false sentence is evidence, and evidence is what this permission runs on. Check
the claim yourself against the live system before you edit, because the peer's
reading of it is not the fact either.

### Beyond the four: the rest of the repo, one approval at a time

**Owner's ruling, 2026-09-14:** *"aide also can update github, and i think the
monitor should also have write access to the repo as long as i approve it."*
**Extended to the aide on the same terms later that day**, answering *"so the
aides won't have write access to the repo?"* — see *The aide* above for the two
things that land differently there.

So the monitor and the aide may write any path in the repository — **and the
second half of that sentence is the whole of the permission.** Read it as a
conditional, not a grant with a courtesy attached. Everything in this section
binds both; where it says *monitor*, an aide should read its own role.

**It is not a fifth carve-out, and the difference is the shape.** The four above
are standing: the monitor overwrites `current_issues.md` every tick and asks
nobody, because asking every tick would be noise. This is the opposite — no
standing permission, one approval per change, and the approval expires with the
change it was given for. An approval to edit one file is not an approval to edit
the directory it is in, and yesterday's approval is not today's.

**Ask before, and ask with the diff already decided.** *"May I write to the
repo?"* is unanswerable and wastes a tick. What the owner needs in order to say
yes or no is the path, what the change does in a sentence, and **why the monitor
rather than the loop** — see the next paragraph, because that third one is the
question that should usually stop you.

**The default is still the loop, and this permission does not compete with it.**
An issue a reviewer's implementing subagent can take should go to the reviewer.
The reason is not protocol tidiness: a monitor edit approved by the owner skips
the diff read the coder does at a merge, and that read is the property this loop
exists to have. *"The reviewer is busy"* is not a reason; *"the reviewer cannot
reach this"* is. The cases that qualify look like:

- the loop's own machinery is broken in a way that blocks the loop from fixing
  it — #594 is the worked example, a hook config the sessions cannot load;
- a one-file config or wiring change where a PR's review surface is the same
  line the owner already read in the request;
- a correction in a file only the monitor is rooted to see is wrong.

**Everything else about the monitor's writes is unchanged.** Scope the commit
with a pathspec and never `git add -A` — the coder owns the working tree and it
is normally dirty. **Name the approval in the commit message**, with its date and
the owner's words, for the same reason a factual correction names what was false:
an approval nobody can find is indistinguishable from a permission taken.

**And the guard does not see any of this.** Every pattern in `WRITE_PATTERNS`
(`role_guard.py:116-124`) begins `\bgh\s+`, and the hook's matcher is `Bash`, so a
`git commit`, a `git push` and an editor write all pass untouched. The guard does
run `git` itself — `git show` at a SHA, `git status --porcelain`, `git rev-list`,
`git diff-tree`, in the merge gate at `:706-899` — but those are its own probes,
not an inspection of what the session typed. Every sentence above is held by the
monitor having read it, which is the argument for asking when the answer is not
obvious rather than for deciding carefully in private.

**Committing is the part that was missing, and it is not optional.** The
coder's skill tells it to scope its commits to the project directory, and
nothing checks that it did — every `role_guard.py` write pattern begins `\bgh\s+`
and none of them matches a `git` command line, `.git/hooks/` holds no
non-`.sample` file, and `.gitignore` says nothing about `.claude`. So the coder
is *supposed* not
to pick `.claude/` up, which is a weaker thing than cannot, and it is the reason
the next paragraph says promptly. Before this ruling nobody could commit
`.claude/` at all. That is #255: eight files and 473 lines sat in one working tree for
24 commits and about ten hours, one `git checkout -- .` from gone, and with no
SHA the reviewer had to audit the working tree — the exact thing *Audit at a
commit, never at the tree* exists to prevent. So:

```bash
git add -- .claude/          # pathspec, always. never `git add -A`
git commit                   # the coder's uncommitted work is in the same tree
git push
```

**Scope every command to `.claude/`.** The coder owns the working tree and it
is normally dirty with in-flight work; a bare `git add -A` from the monitor
sweeps somebody else's half-finished change into a commit about skill prose.

**`.agents/` is covered too, since the owner extended the carve-out on
2026-09-11 and `34e9588` tracked it.** It was excluded before that, and for a
concrete reason worth keeping: `.agents/rules/global.md` was a symlink to
`/Users/user/.claude/CLAUDE.md`, an absolute path outside the repo, so committing
it would have recorded a target broken in every other checkout. That is why it
is now a **regular file** carrying a copy of that content — which means the
filesystem no longer keeps the two in sync. Edit `~/.claude/CLAUDE.md` and
`.agents/rules/global.md` has to be updated by hand, or the coder session goes
on following the rule that changed. `rules/project.md` is a relative symlink
(`../../CLAUDE.md`) and needs nothing.

### Skill lifecycle and session restart discipline

**Role sessions cache skill text at session startup.** Neither Claude Code nor
`agy` reloads `SKILL.md` or rule files dynamically into an already-running
session when they are edited on disk. Therefore:
- Editing a skill or rule file in the repository changes the instructions for
  **future sessions**, not running ones.
- When an issue modifies a `SKILL.md` file or loop rule, the change does not
  take effect in existing long-running loop sessions until those sessions are
  restarted.
- If a session demonstrates behavior from an older skill version (e.g. missing
  new gates or formatting rules), restart the session so it picks up the current
  HEAD instructions.

**`/tmp` is the one path you may write to.** Investigation needs somewhere to
put a probe script, a reproduction, a captured log, a file checked out at an
older SHA, a prototype of the fix you are about to recommend. All of it goes
under `/tmp` and none of it in the repo. This is not a grudging exception: a
reviewer that cannot run an experiment reports guesses, and a recommendation
nobody executed is worth far less to the coder than one that was. Never copy
anything back.

**Prefix every path with your role, because `/tmp` is shared and the other
session is writing there too.** `rev-` for the reviewer, `cod-` for the coder,
`mon-` for the monitor, `tst-` for the tester, then the issue number, then what
the file is:

```
/tmp/rev-59-repro.py      /tmp/rev-59-body.md      /tmp/rev-59-mut.py
/tmp/cod-59-comment.md    /tmp/tst-weather.py
```

Both sessions reach for the same obvious names otherwise, and the loser finds
out by reading someone else's work back. Measured here on 2026-09-09:
`/tmp/i59.md` was written by the reviewer as issue #59's body and silently
overwritten by the coder with its decline comment for the same issue. Nothing
warns you — the read succeeds and returns the wrong document.

Corollary: the working tree belongs to the coder and is usually dirty with
in-flight work. Audit at a commit, never at the tree — `git show <sha>:./path`.
Redirect it into `/tmp` if you need to run it.

### What you never run, and never print

Read-only is about more than files. Some of what is in this repo reaches off the
machine when you execute it, and a reviewer that runs those has stopped being an
observer of the system it is auditing.

| Never run | What it reaches |
|---|---|
| `tools/env_check.py` (the coder excepted) | Its last block deliberately probes with no `GOOGLE_APPLICATION_CREDENTIALS` set, so `google.auth` goes out to the GCE metadata server at `169.254.169.254` — see `env_check.py:540-556` and the docstring at `:29-31`. Every other case in the file short-circuits before that; this one exists to measure the trip. |
| `gws`, and anything importing `live/tools/gws.py` | It authenticates as the owner and reads their real calendar, mail and Drive. |
| Anything that **writes** to the Pi (the coder excepted) | installing or copying files, starting, stopping, enabling or restarting a unit, **any `sudo` you typed yourself**, rebooting, editing its `.env`. Deploying is the coder's, and the device is the owner's. Reading it is allowed, and `tools/pi_deploy.py check` is allowed even though it contains a `sudo` — see below. |

**Reading the Pi is allowed, and usually better than guessing.** A read-only
ssh — `systemctl --user is-active`, `journalctl`, `loginctl show-user`,
`vcgencmd`, `cat` of a unit or a conf — settles claims that would otherwise go
under *Not checkable from here* as guesses. `tools/pi_deploy.py check` is
exactly this and is safe to run. Prefer it to hand-rolled commands, because the
probes are already reviewed. Owner's ruling, 2026-09-10.

**It contains a `sudo`, and that does not make it a write.** One probe reads the
microphone bias register — `pi_deploy.py:673-674`, `sudo i2cget -f -y 1 0x18
0x19`. What is blessed here is *the reviewed probe set*, not `sudo`: a `sudo`
you typed yourself is still forbidden, whatever the verb after it. The line the
repository draws is by verb, and it is asserted rather than promised —
`WRITE_TOKENS` (`:685-690`) holds `i2cset` and not `i2cget`, `mutating()`
(`:751-772`) reads the token back off the command rather than trusting a flag
beside it, and `deploy_check.py`'s **`"nothing in the check plan writes to the
Pi"`** case fails the suite if any step in the `check` plan is mutating. So *the
check plan writes nothing* is checked on every sweep. Cited by case name rather
than line: this was `deploy_check.py:945` until 2026-09-11, by which point `:945`
was a comment about TLS redirects and the case had moved to `:1307` (#273).
Owner's ruling, 2026-09-11, recorded on #240.

**The host is `raspberrypi.local` from here, not `pi2`.** `pi2` resolves on the Pi's own
LAN and does not resolve from the Mac these sessions run on
(`README.md:1604-1606`). `pi_deploy.py` already knows: `resolve()` at
`:1076-1091` tries the bare name, then `.local`, and dies with a sentence naming
both if neither answers. **A hand-rolled `ssh` bypasses that resolver**, which is
the real reason to prefer `pi_deploy.py check` over typing your own probe.

Get this wrong and the failure lies to you. `ssh: Could not resolve hostname
pi2` reads as *the Pi is unreachable from this session* rather than *that is the
wrong name*, so the honest response is to write the claim off as **Not checkable
from here** — and the section that exists for claims nobody can settle quietly
fills up with claims a working hostname would have settled. Measured on
2026-09-10: the reviewer and the monitor made that exact misread on separate
ticks, off six examples in these files that all said `pi2`. Fixing the examples
is why this paragraph is here; fixing the reflex is why it names the error
string.

**The one trap, and it is specific to this device.** *An ssh login is itself a
state change when `Linger=no`.* Without lingering, systemd starts the user
manager at first login and tears it down when the last session exits — so
`systemctl --user is-active` over ssh can report `active` for units that an
untouched boot never started, and the probe manufactures the very thing it is
checking. That is precisely what #10's *reboot and touch nothing* test exists to
catch, and pi2 read `Linger=no` before `pi_deploy.py` began setting it.

Two rules follow:

- **Probe `loginctl show-user <user> -p Linger` before believing any
  `--user is-active` result**, and report it beside the result. `Linger=yes`
  makes the reading trustworthy; `Linger=no` makes it meaningless.
- **Never probe during an acceptance-test window.** When the owner is running an
  unattended-boot test, the run belongs to them from `reboot` until they paste
  the output. Reads before and after are fine; a read in the middle is a write
  to their result.

**Beyond `gh` against this repo and read-only ssh to the Pi, no network.** No
model call, no fetch, no
package install, no reaching a live service to see what it returns. Everything a
verdict rests on is in the checkout, in the installed packages, and in `/tmp`.
When a claim genuinely needs the network, it goes under *Not checkable from
here* — that section is the release valve, and using it is not a failure.

**Never print a secret value.** `.env` here holds real API keys. Checking that a
variable is *set*, or that its shape is right, is fine; echoing the value is not
— not to the terminal, not into an issue or a comment, not into a subagent
brief, and not as an argument to a command whose output you will quote. A key
that reaches a GitHub comment has to be rotated, and the comment history keeps
it after the edit.

**The trap is a read, not a write, and this project has a live one.**
`GET /api/config` on the Pi returns `maps_api_key` in the clear — it has to,
because the browser needs it for Maps JS. So `curl -s
http://localhost:8000/api/config` over the read-only ssh you are now allowed
prints a real key into the transcript, and it looks like an innocent health
check. #10's own step 2 says *"with `maps_api_key` redacted"*, which is the
warning, one step away from the command. Measured on 2026-09-10: the monitor
ran it unredacted anyway.

**Do not reach for `jq` here — pi2 does not have it.** In
`curl … | jq 'del(.maps_api_key)'`, `jq: command not found` means the pipe fails
*after* `curl` has printed the whole object, key included. A redaction that fails
open is worse than no redaction, because you stop watching. Use a filter that is
on the box and prints only what you asked for:

```bash
ssh raspberrypi.local 'curl -s localhost:8000/api/config | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(d[\"revision\"]); print(sorted(d))"'
```

That prints the revision and the key *names*, never a value.

**Better still, do not ask the endpoint.** If what you want is *which build is
running*, `/api/config` is a weak answer carrying a strong risk: the field is
written by hand into `revision.conf` (#155), so it is a label rather than
evidence, and reading it puts a real key one pipe failure away from your
transcript. Two reads settle it without touching the key at all:

```bash
ssh raspberrypi.local 'systemctl --user show ai-terminal -p Environment'  # the claimed sha
ssh raspberrypi.local 'sha256sum ~/ai-terminal/src/ai_terminal/static/css/style.css'
git show <sha>:src/ai_terminal/static/css/style.css | shasum -a 256
```

Matching hashes prove the deployed *content*, which is what you actually wanted
to know. This is how pi2 was shown to be serving `5a1bea2` on 2026-09-10 rather
than trusted to be.

**Before running any command over ssh, ask what its output contains, not just
what you are looking for in it — whether every tool in your pipe exists on that
host, and whether you need that command at all.**

### Images are evidence, and every role may use them

Owner's ruling, 2026-09-13: *"all roles should utilize images for debugging and
improving the app otherwise they are blind to understand what's the issue,
especially on the UX issues."*

Read a screenshot when one exists, take one when you can, and attach it to the
issue or comment where the finding is. This applies to the coder reproducing a
defect, the reviewer checking that a fix looks right on the screen, and the
monitor judging whether an issue describes something a person would mind — not
only to the tester. The case that earned it is #476: the dynamic starter chips
were offering street addresses and a daycare after a weather question, every
session had the DOM and the source, and none of them saw it. One screenshot made
it obvious in a sentence.

**Quote the DOM text or the log line as well, not instead.** The text is what
becomes a check case; the picture is what tells everyone the case is worth
writing.

Two limits:

- **Never a secret in an image.** A `maps_api_key` rendered on the settings
  sheet is a key, and unlike text it will not turn up in a grep when you go
  looking for what leaked. Crop it, or do not take that shot.
- **This repository is public.** Publish only synthetic test data. Do not
  attach household agendas, mail subjects, locations, conversations, or secrets.
  Inspect screenshots and logs for private data before posting them.

**All of this section goes at the top of every brief you write, verbatim**, from
[`references/subagent-brief.md`](references/subagent-brief.md). A subagent has
none of your context and will cheerfully run `env_check.py` because the sweep
instruction told it to run `tools/*_check.py`. Paste it rather than retyping it
from memory, which is how one gets dropped: on 2026-09-09 the brief for #71 lost
the parent-SHA sweep exactly that way. **Every later section that calls for a
brief means this one**, and none of them repeats the reason.

## Labels

**One mechanism per axis, and never two axes in one name.** Labels are a flat
bag of strings, so a scheme that puts routing, classification and workflow state
in the same bag reads fine to a human and is unqueryable by a session: nothing
says which of them are mutually exclusive, and nothing says which are missing.

Seven axes, four of them labels:

| Axis | How many at once | Mechanism |
|---|---|---|
| app | exactly 1 | label, unprefixed: `ai-terminal` |
| type | exactly 1 | label, `type:` |
| priority | exactly 1 | label: `P0` `P1` `P2` |
| stage | exactly 1 while open | label, `stage:` |
| flag | 0 or more | label: `on hold`, `hold:monitor` |
| ordering | 0 or more | **issue dependency**, not a label |
| closing reason | 1 once closed | **GitHub's own `state_reason`**, not a label |

The two non-label rows must not become labels. Ordering as prose or as a label is
invisible to the pick query, and a closing reason as `wontfix` duplicates a field
GitHub already sets, renders, and returns on every read.

Priority keeps no prefix: it is queried by shape (`^P[0-9]$`), and three
two-character labels do not need namespacing to read as one axis.

**What each axis means, and how to pick a value**, is in
[`references/labels.md`](references/labels.md): the app label and how it is
derived from the working directory, the P0/P1/P2 test, the five types and the
`type:spike` filing gate, closing reasons, and how to record ordering as a
dependency. Read it before you file. The two axes below are here instead because
they are read on every tick rather than at filing time.

**Who may set what.** Five actors write to this repo — the owner and the four
sessions — and on GitHub they are usually the *same login*, so nothing in the
audit trail tells them apart. These used to be conventions,
which meant breaking one was invisible and enforcement depended on whether a
session had read this file and remembered it. **Since 2026-09-10 they are
enforced by a hook** — see *The guard* below. Read them anyway: the guard
refuses the write, it does not tell you what to do instead.

| Label | Set by | Cleared by | The other roles |
|---|---|---|---|
| app label | whoever files | nobody | filters every query on it |
| `P0` / `P1` / `P2` | reviewer at filing; monitor on moderation; tester and aide at filing only; owner overrides | owner, or reviewer or monitor with a stated reason | **the coder never re-prioritises**, and **the tester sets one at filing and never moves it after.** Lowering a priority to skip work is indistinguishable from triage, and neither the tester nor the aide sees the queue at all |
| `type:*` | whoever files | reviewer, when the diagnosis moves it | coder does not relabel; say it in the closing comment instead |
| `type:spike` | reviewer | — | it constrains what may be filed; see `references/labels.md` |
| `stage:triage` | whoever files; coder on PR refusal handback | reviewer, when it posts the analysis; monitor; aide | coder swaps `stage:pr` to `stage:triage` on PR refusal handback (#654); tester sets it at filing and never removes it; aide files and may clear on manual triage |
| `stage:ready` | **reviewer only** | reviewer, dispatching an implementer; monitor on close | the reviewer's statement that the analysis is done and an implementer can start; no PR exists yet |
| `stage:pr` | **reviewer only**, when its subagent's PR is open and green | **coder only**, at merge or refusal handback; monitor on close | the one label the coder clears (or swaps to `stage:triage` on handback, #654). Replaced `stage:doing` on 2026-09-11 |
| `stage:verified` | **reviewer only** | reviewer, on reopen **and off its per-tick reopen sweep** — a reopen by the owner or the aide does not pass through the reviewer, and the label survives it | the coder never applies it — it is the reviewer's verdict, and a coder that self-certifies removes the check the loop exists for |
| `stage:owner` | any of the three | any of the three, once the owner has answered | says *understood, and waiting on the owner*. Unlike `on hold` it is not a permission, so whichever session gets the answer clears it |
| `on hold` | **owner only** | **owner only** | coder skips it; reviewer does not chase it and does not file work that depends on it |
| `hold:monitor` | **monitor only** | **monitor only** | its PR does not merge and `gh issue close` is refused while it is on — but GitHub's own auto-close on merge is invisible to the guard (rule 7). Read the latest `<!-- monitor -->` comment; argue on the thread |
| `needs:live` | reviewer, monitor, tester or aide, at triage | **monitor only** | says the acceptance cannot be settled from any `tools/*_check.py`. Its PR writes `Refs #N`, not `Closes #N`, and the merge is refused if it would close the issue. Taking it off is the bypass, which is why it is the moderator's |
| `stage:live` | **coder**, at the merge, in place of clearing `stage:pr` | reviewer or monitor, when the issue closes | merged and deployed, and the live check is owed. One of two stage labels the coder applies (with `stage:triage` on handback, #654) |

### `hold:monitor` — the monitor contesting a patch

Added 2026-09-13 on the owner's decision (#490), after this was measured on #476:

| Time (UTC) | Event |
|---|---|
| 21:41 | Monitor comments *Hold this patch — it rebuilds the thing #454 was opened to remove*, and names the function not to merge |
| 21:45 | PR #484 is opened, containing that function |
| 21:53 | Merged, issue closed `COMPLETED` |
| 21:54 | Deployed to the Pi |

Nobody did anything wrong that can be pointed at. **The loop's start signals are
labels and its reasoning is comment bodies, so an objection written as reasoning
is structurally invisible to the thing it objects to.** Whether the hold was
right does not enter into it: a wrong one would have been merged through just as
fast, which is the tell that this is a protocol defect rather than a judgement
one.

What it means, and the three things it is not:

- **It is not `on hold`.** That one is the owner parking an issue, and it is the
  owner's alone. This one is one session saying *not this patch, and here is
  why*. They lift on different evidence and by different hands.
- **It is not a veto.** It buys a round trip, nothing more. Argue on the thread;
  the monitor is obliged to answer and to lift a hold it cannot defend, and a
  hold that sits unanswered for a tick is one the monitor should be clearing
  itself.
- **It is not a substitute for a dependency.** Ordering stays an issue
  dependency. This says *the proposed change is wrong*, not *do this one first*.

The monitor states, in the comment it posts with the label, **what it is
contesting and what would lift it**. A hold with no stated exit is the stale
guidance problem in a new costume, and the monitor's own skill already calls
that the worst failure available to it.

### The guard — these are permissions now

`.claude/hooks/role_guard.py` runs on every shell call and refuses GitHub
**writes** that break the table above. Reads are untouched, so investigation is
never slowed down. ([`../../hooks/README.md`](../../hooks/README.md) is the
owner's side of this: how each session is launched into its role and what needs
a restart.) It enforces eight things:

1. **No role, no write.** A session whose `CC_ROLE` is unset cannot create,
   comment, edit, close or `gh api -X POST` at all. This is the admission
   control the loop never had.
2. **Every comment carries its marker.** `<!-- monitor -->`, `<!-- reviewer -->`,
   `<!-- coder -->`, `<!-- tester -->`, `<!-- aide -->` — checked in the command
   line and in any `--body-file`. The
   rule is scoped to comments, not to writes: a bare `gh issue close N` or
   `gh issue reopen N` carries no text and so cannot carry a marker, and is left
   alone. Add `--comment` to either and the marker is required again.
3. **A comment body the guard cannot read is refused, not waved through.** Since
   2026-09-13: `--input -`, `-F body=@-`, a stdin pipe, or a heredoc fed straight
   to `gh` are all refusals, and so is a comment with no body flag at all. Until
   then they were an intentional fail-open gap (#314) — until it turned out to be
   firing about five times a day, on the loop's *highest-authority* comment class,
   the post-merge verification verdicts (#470). Write the body to
   `/tmp/<role>-<N>-body.md` and pass `--body-file`; **the heredoc-then-file shape
   everyone already uses is fine**, because what the guard reads is the file.
4. **Restricted labels match the role.** `stage:ready` and `stage:verified` are
   the reviewer's (`stage:ready` and `stage:pr` may also be cleared by monitor
   on close), `stage:pr` is the reviewer's to apply and the coder's to
   clear, `on hold` is nobody's, and the coder cannot re-prioritise.
5. **Only the coder merges.** `gh pr merge` is refused for every other role. The
   reviewer opens PRs and reviews them; landing one is the coder's alone.
6. **Neither the tester nor the aide closes.** `gh issue close` and the REST form
   (`state=closed`, whichever way it is spelled) are refused for both. They file
   what they saw and comment when a run starts passing; whether the issue behind
   it is done is a judgement from the backlog, and neither of them ever sees the
   backlog.
7. **`hold:monitor` stops a `gh issue close` and a `gh pr merge`.** Since
   2026-09-13: `gh issue close N` on an issue carrying it is refused, and so is
   `gh pr merge` on a PR that GitHub reports as closing it — for every role, the
   monitor included. The PR-to-issue mapping is the API's
   `closingIssuesReferences` (`role_guard.py:643-659`), not a scan of the PR
   body and not the branch name. This is the one rule that costs a network call,
   so it runs last and only on a close or a merge that every other rule has
   already allowed (measured at 0.55 s against live GitHub; a comment, which
   never triggers it, stays at 0.03 s). It fails open on anything it cannot
   answer — `gh` missing, offline, timed out — because a guard that stops the
   loop when GitHub is slow costs more than the rule saves. The sanctioned way
   through is to lift the hold in the same command
   (`gh issue edit N --remove-label hold:monitor && gh issue close N`), which
   only the monitor can do.

   **It does not stop GitHub from closing the issue itself, and on 2026-09-13 it
   did not.** `role_guard.py` is a `PreToolUse` hook: it reads a shell command
   string and nothing else. When a merged PR's body says `Closes #N`, GitHub
   closes `#N` server-side with no command anywhere for the hook to inspect. That
   is how **#558** closed at 15:43:21Z still carrying the label — the timeline's
   `ClosedEvent` names `closer: PullRequest #560`, not a `gh issue close`. So the
   close half of this rule is only as strong as the merge half: **stop the merge
   and the close never happens; miss the merge and the close is unstoppable.**
   Nothing in the hook layer can change that, and `role_guard_test.py` has no
   case for it because there is no command to write a case against.

   **Two known ways past the merge half.** `gh pr merge` with no number, run from
   a branch that is not the PR's, makes `gh pr view` error and the check fails
   open (`role_guard.py:647`). And any merge that does not go through a hooked
   tool — a web-UI click, a CLI whose hook did not fire — is never seen. Whether
   #560's merge took one of these is unsettled: the guard, replayed against the
   real #560 and #558 in both payload shapes, still returns `block` and `deny`,
   and the `agy` log records no tool calls either way.

8. **`needs:live` stops the *close*, at both ends.** Since 2026-09-14: a
   `gh pr merge` whose PR would close an issue carrying `needs:live` is refused,
   and so is `gh issue close` on such an issue while no comment on the thread
   carries a `live-verified:` line. Same `closingIssuesReferences` lookup as rule
   7, same fail-open on anything GitHub cannot answer, same escape hatch — drop
   the label in the same command. The difference is what it is protecting: rule 7
   stops a change the monitor is contesting, this one stops an issue leaving the
   queue before anybody has looked at the thing it describes. See
   [*`needs:live`*](#needslive--the-merge-does-not-close-it).

   **This one does not have rule 7's server-side hole**, and that is the reason
   it lives at the merge rather than at the close. GitHub's auto-close is
   invisible to a `PreToolUse` hook, so the only moment the loop can refuse it is
   before the merge that triggers it — which is exactly what the gate does, by
   reading the same field GitHub itself acts on.

**One guard, two CLIs.** It is wired by `.claude/settings.json` on Claude Code
and by `.agents/hooks.json` on the Antigravity CLI, reads either payload shape,
and refuses with `block` on the first and `deny` on the second. Nothing above
depends on which: the rules, the markers and the labels are the same either way.
If you are refused, the wording will be identical; only the field says which CLI
you are on.

**If the guard refuses you, the answer is to ask the owner which role this
session is** — not to work out which one you must be. Deciding that from a
document is the exact failure the guard exists to stop, and it is why *Which
role you are* is the first section of this file.

> **Why it exists.** On 2026-09-10 three role violations happened in one day and
> none was caught at the time. The monitor applied `stage:ready` to #10. The
> coder applied `on hold` to the same issue, needing the effect and having no
> correct lever. And a fourth session — started with the words `read this
> project`, which is the entire instruction it was given — read this skill,
> concluded it was the reviewer, filed #155 unsigned with a reviewer-only label,
> and exited four minutes later. Nobody noticed until the missing marker was
> queried, and the first hypotheses were all wrong because the timeline could not
> say who acted.
>
> The lesson is narrower than "sessions misbehave". **A convention documented in
> a skill is enforced by whether a session read the skill, and the session most
> likely to break it is the one that inferred its role from that skill rather
> than being given one.** Making the convention louder inherits the defect;
> moving it into the harness does not.

**It is not a security boundary and does not pretend to be.** A session that
wanted a role could export `CC_ROLE` itself, exactly as it could have typed
another role's marker. It stops the session that never considered the question,
which is every case measured so far. It also fails open if it crashes: a guard
that bricks the loop over an unusual quoting style would cost more than the gap
it closes.

**Subagents inherit the parent's `CC_ROLE`.** The guard cannot tell them apart,
so *No GitHub writes* stays in the brief and stays the subagent's rule.

### Stage — where the issue is in the loop

The axis that says whose turn it is. Exactly one `stage:` label at any moment,
and an issue with two of them is a bug in the loop, not a state.

| Label | Means | Whose turn |
|---|---|---|
| `stage:triage` | Filed. Nobody has traced it to a line yet, and no check reproduces it | reviewer — fan out an analysis subagent, then write the reproducing case |
| `stage:ready` | The cause is traced to a line, the approach is decided, and **a check reproducing the defect has been written and shown failing**, so an implementer can start without re-analysing. A patch on the thread is welcome and not required. No PR yet | reviewer — fan out an implementing subagent |
| `stage:pr` | A pull request is open, with 22/22 green on its branch | **coder** — read the diff, merge, deploy |
| `stage:live` | Merged and deployed, and the issue is **still open** because it carries `needs:live` and its PR said `Refs #N`. See [*`needs:live`*](#needslive--the-merge-does-not-close-it) | **tester** — run it on the Pi or against a live model and post what you saw |
| `stage:owner` | Traced and understood, and the next move is a decision only the owner can make | owner |
| *(closed `COMPLETED`, not `stage:verified`)* | Merged. Nobody has tested the merged result | reviewer |
| `stage:verified` | Closed, and the verification subagent passed it | nobody; done |
| closed **and** `stage:pr` | Not a state. The merge closed the issue and the label was never cleared | reviewer, after one tick |
| **open** and `stage:verified` | Not a state. A reopen restores the labels the issue closed with, and no pick query selects this | reviewer, off its per-tick reopen sweep |

#### What `stage:ready` requires, and what it does not

Four conditions. Three are the reviewer's own claim; the third is the only one
that is not an opinion. The label goes on when all four hold and comes off when
a PR opens.

1. **The cause is traced to a line.** `app.js:340`, not *somewhere in the
   front-end*. If the thread cannot name the line, the issue is still
   `stage:triage`.
2. **The approach is decided.** One way forward, chosen. A body that lists three
   options and their trade-offs without picking one is `stage:owner` if the pick
   is a preference, and `stage:triage` if it is not.
3. **A check reproduces the defect, and has been shown failing.** See
   [*The reproduction gate*](#the-reproduction-gate) below — this is the one
   condition nothing else in the loop can fake.
4. **No PR is open yet.** The moment one is, the issue is `stage:pr` and the
   coder's.

**A patch on the thread is welcome and not required**, and so is a *case list* —
exhaustive coverage of the area is somebody else's job. What condition 3 asks
for is one case, the one that goes red today.

And what the label is *not*: it is not a record of effort spent, and an issue is
drained from the queue when the label moves, not when a comment lands on it.

**`stage:ready` used to require a patch on the thread, and that sentence was
wrong by 2026-09-14.** Corrected by the monitor on that date against 265 closed
issues that passed through the label: 95 of them (36%) ever carried a patch, and
the rate fell 96% → 49% → 23% → 24% → 14% across 09-09 to 09-13. The break is
2026-09-11, the day the reviewer began handing `stage:ready` to an implementing
subagent instead of to the coder — the dispatch target was updated and the patch
requirement was not. Conditions 1 and 2 above were implicit in the old sentence
and are now written down, so dropping the patch requirement did not loosen the
label; it replaced one requirement nobody met with two the implementer actually
depends on. The owner approved the wording on 2026-09-14. **Restoring the patch
requirement is a one-line revert** if that turns out to be wrong.

#### The reproduction gate

**Owner's rule, 2026-09-14.** *"For any open issue with triage, the reviewer must
write a test to reproduce it, confirm it fails, and make sure the PR makes it
succeed."*

So `stage:triage` → `stage:ready` is no longer a judgement the reviewer can make
from reading alone. It requires an artefact, and the artefact has to have been
seen red.

**The three steps, in this order, and the order is the whole point.**

1. **Write the case, against the behaviour the issue asks for** — not against the
   behaviour the code has. Take the wording from the issue's *Acceptance*
   section. If `Acceptance` says *"the clock face names the place"*, the case
   asserts a place name; it does not assert *"place is not the empty string"*,
   which is the symptom restated.
2. **Run it at the current `main` and paste the failure on the thread.** The SHA,
   the check script, the case name, and the actual red output. A case that has
   never been seen failing is a case nobody has shown to be about this issue.
3. **The PR turns it green without touching its expected value.** Adding cases is
   fine. Changing what the reproducing case expects is not a matter for a PR —
   it means the issue's `Acceptance` was wrong, which goes back to the thread.

**Why step 3 has a rule of its own.** Because the failure it prevents has already
happened, in the PR that was supposed to be the fix. On 2026-09-14, PR #632 for
#465 did this, in the same diff as the change:

```diff
-check("with only HOME_COORDS set, district is reverse-geocoded",
-      screensaver.where(), (35.232, 139.107, "Hakone"))
+check("with only HOME_COORDS set, coordinate string is used directly",
+      screensaver.where(), (35.232, 139.107, "35.232, 139.107"))
```

The expected value was rewritten to the value the new code produced. The suite
reported 22/22 green, the issue closed one second after the merge, and the owner
reopened it thirteen minutes later because the clock face was showing raw
coordinates — **the exact string the check had just been taught to expect.** The
issue's own `Acceptance` line, *"with only `HOME_COORDS` set, the clock face
names the place"*, was sitting in the body the whole time, and the new case's own
title reads as its negation.

That is the third time this shape has been measured: #488 (*"js_check pins
#481's defect string as expected"*), #580, and now #465. It is not carelessness.
It is what happens when the same hand writes both the behaviour and the
statement of what correct means, in one diff, under one green number.

**When you cannot write the case.** Say so on the thread, in that sentence,
with the reason — and the issue does **not** get `stage:ready`. The honest cases:

| Situation | Where it goes |
|---|---|
| A feature request with no defect to reproduce | `stage:ready` is fine. Write the case for the behaviour being *added*, red because the feature does not exist yet. That is the same gate, not an exemption |
| Only reproducible on the Pi, or against a live model | Still `stage:triage`, **and label it `needs:live`** — see [*`needs:live`*](#needslive--the-merge-does-not-close-it). Hand it to the **tester** — that role drives a real browser and reads the Pi. Open the comment with a line reading exactly `needs: tester repro`, then name what device or model state is needed; that line is what the tester searches for. The gate is satisfied by the tester's failing run on the thread, not by a `tools/*_check.py` case |
| About the loop or the process itself, not the product | `stage:ready` with the gate waived, stated explicitly as waived. #629 is the standing example |
| You simply could not think of one | **`stage:triage`.** This is the answer that matters. An issue nobody can write a failing case for is an issue nobody has understood, and moving it on is how it comes back |

**The explicit trigger for `needs:live` (#652).** Any acceptance criterion
that cannot be executed offline by `tools/*_check.py` (hardware, physical 10"
touchscreen kiosk usability, microphone, camera, real reboot/GPIO, live
model/OAuth network rounds) **MUST** have `needs:live` attached. Unit tests
covering logic components (e.g. 23 cases in `tools/js_check.py` for modal DOM
structure and listeners) **DO NOT** exempt an issue if any live criterion
remains. The gate checks whether the full acceptance is settled offline, not
whether part of the patch has a passing unit test.


**Since 2026-09-14 the PR end of this is enforced, not written down.**
`role_guard.py` refuses `gh pr merge` unless the PR body carries
`- **Fail-before:** tools/<name>_check.py <N> -> <M>` with `M > N`, or one of
four explicit waivers — `needs: tester repro`, `loop/process`, `docs only`,
`owner ruling`. It then checks the claim against git: it counts `check(` calls in
the touched suites at the fork point and at the head, and refuses when the number
did not rise, because a check file that changed lines without gaining a case is
an expectation that got rewritten.

Why it is a hook and not a paragraph: across the last 25 merged PRs, `Sweep SHA`
— which this hook already refused a merge over — was present in 24, and
`Fail-before`, which every skill in this loop has asked for since the beginning,
in 7. Same week, same authors. **The waiver is a line somebody types, and that is
a real hole**; it is there because a gate with no waiver wedges the loop on the
issues that genuinely cannot carry a case, and a wedged gate gets switched off.
The monitor audits which waivers get used, each tick.

**This is a brake, and it is meant to be.** The measured loop speed on #465 was
twenty minutes from issue to merge, against the only verification channel that
ever caught anything — the owner looking at the screen. A gate that slows the
first number is buying agreement with the second.

**Why the merge closes the issue rather than the reviewer closing it by hand.**
The PR's commit carries `Closes #N`, so merging closes the issue, and the
reviewer's detection query — *closed `COMPLETED`, no `stage:verified`* — picks it
up unchanged. That query has three separately measured defects fixed in it
(#211, #264) and routing around it to invent a `stage:merged` state would have
thrown all three away for no gain. The reviewer still does the testing, which is
the part the owner asked to move; only the keystroke that closes the issue
belongs to the merge.

**That holds for everything a suite can check, and for nothing else.** The
exception is the next section.

#### `needs:live` — the merge does not close it

**Owner's ruling, 2026-09-14**, on #629. Every gate above this one ends at a
`tools/*_check.py` case, and the whole point of #629 is the class of issue where
no such case exists: the behaviour is only observable on `raspberrypi.local`, or against
a model that actually answers. For those, closing on merge is not a convenience,
it is the defect.

The number: **of 178 issues closed here, 171 closed within ninety seconds of
their merge.** Everything this loop has ever written about verifying afterwards
is a rule about work that begins after the issue has left every queue, and it is
skipped at the rate you would expect from that. Median verification comment
length collapsed from 7 900 characters to 381 on 2026-09-11, and no comment since
09-12 mentions a red-before run at all.

So the close moves to the end instead:

1. **Whoever triages applies `needs:live`** the moment it is clear that no suite
   case can settle the acceptance. Reviewer, monitor, tester or aide; it goes on
   at `stage:triage`, alongside the `needs: tester repro` comment.
   - **Explicit criteria (#652):** Audit every acceptance criterion against
     offline suite capability (`tools/*_check.py`). If **any** acceptance
     criterion cannot be executed offline — physical 10" touchscreen kiosk
     interaction (QR code scannability at mount distance, touch targets with
     fingers, keyboardless kiosk recovery), microphone/acoustics, camera, real
     reboot/GPIO, live Gemini model responses, or live OAuth network rounds —
     `needs:live` **MUST** be attached.
   - **Unit tests for logic components do not exempt:** Unit tests covering
     logic or DOM structure (e.g. 23 cases in `tools/js_check.py`) **do not**
     exempt an issue if any live criterion remains. On #642, green logic tests
     verified the companion modal structure but could not verify QR scanning at
     distance or touchscreen usability; omitting `needs:live` let PR #651 write
     `Closes #642` and auto-close the issue on merge without verification on pi2.
   - **The Dual-Point Check:** Checked at triage, and checked again before PR
     creation. If triage missed `needs:live`, the implementer must write
     `Refs #N` rather than `Closes #N`.
2. **The implementer writes `Refs #N`, not `Closes #N`.** One word, and it is the
   whole mechanism — GitHub populates `closingIssuesReferences` from the closing
   keyword and from nothing else, so `Refs` means the merge leaves the issue
   open. `role_guard.py` reads that field and **refuses the merge** if it would
   close a `needs:live` issue, so this is not a rule anyone can forget.
   Before writing `Closes #N`, the implementer checks the issue's acceptance
   criteria: if any criterion cannot be validated offline, `Closes` is forbidden
   and `Refs #N` must be used even if triage omitted `needs:live`. A PR whose
   `Closes #N` targets an issue with an untestable-offline acceptance criterion
   either carries `needs:live` on that issue or states an explicit one-line
   reason in the PR body for why the criterion is settled offline. A silent
   `Closes` is the defect.
3. **The coder swaps `stage:pr` for `stage:live`** in the same breath as the
   merge. That is the one stage label the coder ever applies, and it exists so
   the issue lands somewhere rather than sitting stageless.
4. **The tester runs it where it can be seen** and posts what it saw, with a line
   of its own reading `live-verified: raspberrypi.local 2026-09-14` — the host or the
   model, and the date. Screenshots belong here; see *Images are evidence*.
5. **Then it closes**, `stage:verified`, by the reviewer. `gh issue close` is
   refused while `needs:live` is on and no comment carries that line.

**The honest way out, when the rig cannot see it either.** Say so on the thread,
take `needs:live` off, and close — `gh issue edit N --remove-label needs:live &&
gh issue close N` is allowed in one command, the same shape as lifting
`hold:monitor`. Only the monitor and the owner may remove it, because removing it
is the bypass and the bypass should cost a conversation.

**What this does not fix.** `live-verified:` is a line somebody types, exactly
like the `Fail-before` waiver, and a session determined to clear its queue can
type it without looking at anything. What the mechanism actually buys is narrower
and worth more: **the issue does not leave the queue at the merge.** A rule saying
*verify later* competes with a forty-five-second cooldown and loses. An issue
still sitting open at `stage:live` does not.

#### The Architectural Intent Guard

When an issue or thread carries explicit owner directives or architectural questions
(e.g. *"Why do we re-derive a synthesized 'short name' through lossy reverse geocoding
heuristics instead of honoring the recognized/entered place name that the user set in the
settings panel?"* in #373):

- **The triage, implementation, and verification MUST confirm that the architectural directive was honored.**
- If the implementation substituted a narrow regex, a heuristic tweak, or a symptom
  patch while bypassing the owner's architectural instruction, the issue cannot be moved to
  `stage:ready` and cannot be closed as `stage:verified`.

**`stage:owner` is not `on hold`.** `on hold` is the owner's instruction to stop
and only they may set or clear it. `stage:owner` is any session's statement that
the queue has run out of moves — the cause is traced, no patch is possible until
a preference is chosen, and neither `triage` nor `ready` is honest. Any of the
three may apply it, and whichever session receives the answer clears it.

**Clear it before closing.** The reviewer's query now asks for *closed
`COMPLETED` without `stage:verified`* (#211), so an issue closed under
`stage:owner` does reach the verify queue rather than vanishing from it the way
#151 did. Clear it anyway: the stage says whose turn it is, and *nobody's* is not
the answer once the issue is finished.

Read down that column and the loop is a ring: reviewer, reviewer, coder,
reviewer. Every queue either session works from is one label plus the app label.
The axis has to cover *open* as well as closed, because without it whether an
issue has been analysed lives in a paragraph on the thread.

**What is *being written right now* is no longer a label at all.** It is an open
pull request, and that is strictly better than `stage:doing` ever was: the PR is
created by the act of doing the work rather than by remembering to say so, it
cannot drift from reality, and it survives a compaction or a restart of the
session that started it. `gh pr list --state open` is the honest answer to *what
is in flight*; `stage:pr` is only the projection of that onto the issue, for the
benefit of queries that read issues.

The three rules that keep it honest:

- **Move the label in the same call that does the work it describes** — take and
  start, close and clear. A stage set afterwards is a stage that will be
  forgotten on the one run that crashes in between.
- **When the close is a `Closes #N` trailer, there is no call to attach it to —
  so the label comes off in the very next command after the merge.** This is not
  a corner case: it is now *every* close, because every close is a merge.
  `gh pr merge` is what closes the issue and it takes no `--remove-label`. So:

  ```bash
  gh pr merge <PR> --squash --delete-branch   # this is the close
  gh issue edit N --remove-label stage:pr     # this is the next command. not the third.
  gh issue comment N --body-file /tmp/cod-N-merge.md
  ```

  **Before the comment, not after it.** Writing the comment first feels natural —
  it is the interesting part — and it is where the gap comes from. Measured
  across the 251 trailer closes of the loop's first week, when the label was
  `stage:doing` and the close was a `git push`: **126 (50.2%) still carried the
  stage at the instant of close**, median 58 s, worst 9 m 26 s. Every one of
  those windows is a tick where the reviewer could have looked. The 07:47–10:28
  run on 2026-09-10 is the counter-example and the proof it costs nothing: 20
  closes, every label cleared within 0–9 s, because the edit went first. The
  mechanism moved from `git push` to `gh pr merge`; the trap did not move.

  If you notice mid-comment that you skipped it, stop and run the edit. A
  half-written comment loses nothing; a forgotten label is invisible.
- **Never advance a stage on someone else's behalf.** If an issue looks ready and
  is marked `stage:triage`, the reviewer has not posted the patch yet, whatever
  the thread appears to say. Read it, do not relabel it.

**The one exception: a closed issue still carrying `stage:pr`.** That is the
coder's last step half-done — the merge closed the issue and the label was never
removed. It is a bug in the loop, not a state. It used to be an *invisible* bug
as well, because the detection query filtered on *no* stage and the issue never
reached the verify queue; #211 fixed that, so the issue is now caught rather than
lost. Measured on 2026-09-09 under the old label, #70 sat in it.

Removing it is not advancing another role's stage, it is clearing a stale one,
so the rule above does not apply — but wait one tick first, because a coder that
is two seconds from clearing it and a coder that crashed look identical. If it
is still there on the next tick, remove it and say on the thread that you did
and why. Then verify it like any other closure.

### `on hold` — the owner's park

*Parked by the owner: do not pick this up, whatever its priority.*

**Only the owner sets it and only the owner clears it.** Neither session may
unpark an issue, however ready it looks, because the park usually encodes
something outside the repo — hardware that has not arrived, a decision the owner
has not made, a direction they are reconsidering. A session that clears it has
overruled the one actor whose reasons are not written down.

It is a filter, not a priority. `on hold` beats `P0`. The coder skips it; the
reviewer neither files work that depends on it nor chases it for verification.

**A hold that is not the label is a hold nobody else can see.** Owners park
issues by speaking to whichever session is in front of them, and that
instruction then lives in one session's context, where it outlives its reason
and is invisible to the other two. Measured on 2026-09-10: the coder passed over
#10 twice in nine minutes while it was P0, `stage:ready` and unblocked, because
it was still keeping a spoken instruction from 2026-09-08. The monitor could see
the skips but not the cause, inferred *"there is nothing here a coder can do"*
from the behaviour, and published that as the finding. It was wrong, and no
amount of reading the queue would have caught it — **the state was in a place
the queue does not reach.**

Two rules, one for each direction:

- **If the owner parks an issue by telling you, ask them to add the label, and
  say on the thread that the hold exists.** You may not set `on hold` yourself,
  but you can refuse to be its only record. A hold worth keeping is worth being
  visible to the sessions it binds.
- **Never explain a skipped issue by inference.** When a session passes over
  something the pick order says it should have taken, the honest report is *the
  coder skipped this and I do not know why* — then ask it, or ask the owner. A
  confident wrong cause is worse than an open question, because it gets acted
  on: here it produced a dependency gate and an `on hold` recommendation for an
  issue whose real state was one question away.

## Dispatching — the reviewer's rule, and the monitor's

The coder is exempt: it is the session that opens files for a living. The other
two are not.

**Your throughput is the loop's throughput.** The coder blocks on your verdicts,
the owner blocks on your filings, and every subagent in flight is waiting for you
to read its report. A dispatcher that is thirty tool calls deep in a file it
could have handed to an agent is a dispatcher that is not dispatching — the
queue drains at the speed of the one session that cannot be parallelised.

**So the default is delegate.** Any investigation that means reading source,
tracing a call path, re-printing cited ranges, sweeping for a name, checking a
library's behaviour, or verifying another agent's claims goes to a subagent.
That holds for chores as much as for verdicts: a chore is small, which is
exactly why it is not worth your context, and small ones batch into one agent.

The line, concretely:

| You do it inline | You delegate it |
|---|---|
| one or two `gh` calls to read an issue or its timeline | anything that opens a source file |
| `gh issue create`, `edit`, `comment`, `close` | confirming the `file:line` you are about to put in a comment |
| reading a subagent's report and deciding | checking whether that report is right |
| `ScheduleWakeup`, and saying what happened | any `grep`, `find` or sweep over the repo |

Three calls is the threshold. If answering something will take more than about
three tool calls, or any of them reads a file, it is a subagent — even when you
are certain you could do it faster yourself. You could; the point is what else
stops while you do.

**Delegating does not mean trusting.** The check on a subagent is another
subagent, not your own re-derivation. Two cheap agents in parallel cost less
wall-clock than one dispatcher reading files serially, and they leave your
context free for the thing only you can do: holding the queue, deciding the
verdict, and signing the comment.

**Fan out, then wait.** Several agents go in one message so they run in
parallel. While they run, do not start doing their work — that is the failure
this section exists to stop. Schedule the next tick and let the notifications
arrive.

## Who runs which suite

**The coder runs nothing.** Owner's constraint, 2026-09-11: the merge path must
not carry the suite. Every check runs on a branch, before the PR exists.

| | Runs | Cost | Catches |
|---|---|---|---|
| implementing subagent, before it opens the PR | **all 22 scripts** — `uv run tools/check_all.py`, in its own worktree, holding the sweep lock | ~97 s | everything, on the branch. This is the gate |
| implementing subagent, same run | the owning script at the base sha as well | 1–19 s | fail-before/pass-after, which needs the *unfixed* tree — and a worktree is the only place that still exists |
| coder | **nothing** | — | — |
| reviewer, per verification | the sweep at the merged sha, on a `git archive` copy under `/tmp` | ~97 s | what merging two green branches produced, which no branch sweep saw |

**22, not 21.** `wiring_check.py` came back into the sweep on the owner's ruling
of 2026-09-11 (#288) after weeks of exclusion on a citation that pointed at the
wrong line. And `env_check.py` is in it now too — it needs a real `.env`, which
in a worktree is a **symlink** to the one on this machine, never a copy. It is
42 of the 97 seconds and it makes a doomed trip to the GCE metadata server; both
are costs, not hazards.

**The sweep is serial on purpose, and that is a constraint on the parallelism
above.** `check_all.py:40-45`: `progress`, `cache` and `browse` assert the shape
of a *timed* run, and "sharing four cores with `env`'s dozen subprocesses is
exactly the contention that turns those into a suite that fails once a
fortnight." So the implementers code in parallel and **sweep one at a time**,
through `/tmp/rev-sweep.lock`. This works only because there is a single
dispatcher and a shared convention; independent peer sessions could never have
agreed on it, and that is one of the reasons the parallelism went inside the
reviewer rather than across sessions.

**Why the reviewer sweeps again after the merge, when the branch was already
green.** Because a green branch is a claim about a base that no longer exists. N
branches cut from the same commit can each pass and still break once merged, and
the merge sweep is the only thing in the current design that looks at what
actually landed. It is also still the answer to *is the PR's number real* — a
number on a PR body is a claim like any other.

**`wiring`'s runtime is a check in its own right.** #287 closed the last path by
which `wiring_check` reached a real `generate_content` (`recap.py:193`), and that
fix does not fail red — if it is incomplete the suite still passes, only slower
and against a live model. `check_all.py:47-51` puts `wiring` among the 18 scripts
sharing 12 s, so tens of seconds means a real call got through. Report the
number, not the colour.

## Patches cross the boundary

An analysis subagent cuts the patch and an implementing subagent applies it in a
worktree, so both halves of this belong to neither. Since 2026-09-11 the boundary
is between two siblings rather than between two sessions, which makes it shorter
in time and no less real: the analysis was written against the tree as it was,
and `origin/main` moves every time another implementer's PR merges.

The project and repository roots are the same. Extract `git archive <sha>`
without stripping path components for both sweeps and patch checks. Apply
patches from that root and verify the resulting diff.

So `--check` is not the gate. Use `git apply -v` and read the output, or check
that the tree actually moved — `git diff --stat` after applying, and a patch
that touched two files says so. The rule generalises past `git`: **a verifier
whose silence and whose success look identical is not a verifier.** It is the
same defect as a fake that cannot fail, one level up, and the reviewer hands
patches across that boundary on every issue.

Cut patch paths relative to the workspace root, the way `git diff` writes them
from there, and say in the issue which root they assume. `--directory=` is the
coder's escape hatch, not a substitute for saying.

**Say which commit the patch was cut at, in the patch itself.** A patch is a
diff against one specific tree, and that tree keeps moving: the coder commits
while the reviewer is still writing. A hunk rejected hours later then looks like
a wrong patch when it is only a stale one, and nothing on the thread tells the
two apart.

So the first line of the fenced block carries the SHA it was written against:

```
Base: 4795ca6
diff --git a/... b/...
```

`git rev-parse --short HEAD` at the moment you cut it. It goes inside the block
rather than in the prose around it, so it survives the copy into `/tmp` — the
prose does not.

That is the whole convention. **Nothing is gated on it.** A patch whose base has
moved is still worth applying, and `git apply -v` is still what decides; the
field only says where to rebase from when it fails. Owner's decision,
2026-09-11, choosing the record over a pre-start check.

Measured that same day: four patches were sitting in `/tmp` for #151, #163 and
#225, two of them on issues labelled `stage:ready`, so the coder could have
picked them up. `git apply --check -v` rejects all four at the repo root, every
one with `error: while searching for` — context mismatch, meaning the base moved
rather than the paths being wrong. Not one records a base, so rebasing them
starts with a guess.

## Signing your comments

**Sign every comment with a role marker**, as the first line, in an HTML comment
so it renders as nothing. **The guard refuses an unsigned comment** (*The guard*,
above), so this is no longer something you can forget — but it still only checks
that *a* marker is there, and it reads your role from the environment, so signing
as yourself is the part that stays yours:

```markdown
<!-- reviewer -->
## Verification
```

The coder does the same with `<!-- coder -->`, the monitor with
`<!-- monitor -->` and the tester with `<!-- tester -->`. All four
sessions authenticate as the same GitHub login, so without this the timeline
cannot say which of them acted:
on 2026-09-08 `on hold` was added to #64 at 22:35:29Z and removed at 23:01:47Z,
both recorded against the one account, and there is no way after the fact to
learn who parked it or who decided it should not stay parked. Labels and closes
cannot carry a marker, so comment on the thread when you change one and the
reason for something surprising will at least exist somewhere.

It is also a query. `gh issue view N --json comments` plus a grep for the marker
separates the two halves of the conversation, which is how you find the coder's
reply to a recommendation without reading the whole thread. **The monitor signed
`<!-- owner -->` until 2026-09-10** — 59 comments still do — so a search of its
history has to match either marker. The old one said the owner had spoken when
in fact a session had, which is why it changed.

**Every claim gets a `file:line`.** Quote the four or five lines that prove the
point rather than describing them. State plainly what you could not check — the
device, the network, the audio, anything needing a real account. An honest "not
checkable from here" is worth more than a confident guess, and it tells the
reader exactly which manual test is still owed.

## The GitHub-observable heartbeat contract

**The metric: the interval between a role publishing a waiting condition and that
role acting again.**

### Why host and process metrics fail
A wedged session (measured on 2026-09-13, #571) does not crash, exit, or consume
excessive CPU. In #571, the reviewer process was up for 16 hours, continued
refreshing its auth token hourly, maintained its open sockets, and held file locks —
yet its turn was wedged in an internal runtime wait for 2 h 44 m, freezing the loop.
Process table checks (`ps`), lock files, and logfile modification times cannot tell
a wedged session from a healthy one. Furthermore, queue depth and staleness
metrics fail to detect dead handoffs (#558: a two-deep verify queue with recent
activity on both entries where the handoff was dead).

The only host-independent, authoritative observer is **GitHub itself**.

### The contract
1. **Publishing waiting conditions**:
   When any loop role enters a wait state (e.g. idle backoff, awaiting subagents,
   awaiting coder merge, or awaiting reviewer decision), it publishes observable
   evidence to GitHub:
   - **On active work**: An issue comment or label update records the handoff
     (e.g., `<!-- reviewer -->` comment noting implementers dispatched or awaiting
     verification).
   - **Waiting condition markers**: Comments or status updates may embed a
     standardized heartbeat marker:
     `<!-- heartbeat role=<role> state=<idle|waiting_subagents|waiting_coder|active> [since=<iso8601>] -->`
   - **Observable GitHub state**: PR creation (`PullRequestEvent`), comment creation
     (`IssueCommentEvent`), and stage label transitions (`IssuesEvent`) all register
     as activity.
2. **Measuring heartbeat intervals**:
   The heartbeat metric is defined as:
   `interval = current_time - timestamp_of_last_published_action_or_wait_condition`
   If `interval` exceeds the role's maximum expected latency without subsequent
   action by that role, the session is in a wedged or stalled condition:
   - **Reviewer with subagents in-flight**: Subagents typically return within
     10–20 minutes. If the interval exceeds **45 minutes** with no subsequent action,
     the reviewer turn is wedged.
   - **Reviewer idle**: With `IDLE_GAP_MAX=1800 s` (30 m), if actionable work has been
     waiting at `stage:ready`, `stage:triage`, or in the verify queue for > **60 minutes**
     and the reviewer has published no activity or heartbeat, the reviewer is stalled.
   - **Coder idle**: If mergeable green PRs have been waiting at `stage:pr` for > **45 minutes**
     and the coder has published no merge or comment, the coder is stalled.
3. **Auditing by Monitor**:
   The monitor inspects these intervals during its half-hourly pass. When an interval
   is exceeded, the monitor reports the stalled role in `current_issues.md` and
   pings for operator intervention.

## Pre-publish collation

**Nothing is published without collation against existing open issues.** Filing a
duplicate issue or publishing an analysis that contradicts an existing
recommendation lands on the coder as instruction ambiguity (#199). Stating "read
the queue" as background knowledge fails because knowing the rule leaves no
record; collation is a required procedure with a mandatory output token.

**Run the collation query before filing an issue or posting triage analysis.**
Search open issues by primary symbol or function name (`_told`, `maps_search`,
`_seen`):

```bash
gh api "repos/:owner/:repo/issues?state=open&labels=ai-terminal&per_page=100" \
  -q '.[] | select(.pull_request|not)
          | select((.title + " " + .body) | test("<primary-symbol>"; "i"))
          | "#\(.number)\t\(.title)"'
```

If the analysis changes or overturns an existing recommendation, query for every
open issue whose body or comments carry the earlier recommendation as well.

**Every new issue body and triage analysis comment must carry the `Collation:` token.**
It is recorded explicitly at the top of the body or comment:

- When matches are found:
  `Collation: checked open issues for '<symbol>'. Matches: #N. Relation: <distinct / supersedes / duplicate>...`
- When no matches are found:
  `Collation: checked open issues for '<symbol>', none found.`

**Never omit the token on an empty result.** An empty result written down
distinguishes "checked and found nothing" from "did not check". An issue or
analysis without the token cannot pass claim-check.

## When you were wrong

You will be, and more often than the rest of these files imply. Three shapes,
and they are not interchangeable.

**The reopen traces back to your own recommendation.** #71's surviving defect
came from the fix the reviewer itself had prescribed on the thread. From the
coder's side a reopen looks the same either way: the same voice saying the same
issue is not done. So **say whose instruction produced it, in the first
sentence of the reopen.** Not as an apology — as routing. A gap the coder
introduced is a gap in their implementation; a gap you introduced is a gap in
the recommendation, which means the *rest* of that recommendation is now
suspect too and they should re-read it rather than trust it. Only one of you
knows which case this is, and it is not them.

**A comment you already published rests on a false premise.** The reviewer's
comment on #72 described `list_sessions` wrongly — `user_id` is optional in ADK
2.8.0. Editing the comment is the wrong repair: the coder may already have read
it, and an edit leaves no trace that the premise moved. **Post a correction on
the thread**, name the claim, give the version and `file:line` in the installed
package, and say what it changes about the recommendation — including "nothing"
if the fix survives. Then, if the issue is at `stage:ready` and the correction
invalidates the patch, move it back to `stage:triage`; a patch built on a false
premise is not implementable, whatever the label says.

**A comment you published was right, and a later decision replaced its premise.**
That is not either case above and must not be recorded as though it were. On
2026-09-10 the reviewer argued #144's debug port should be opt-in, because a
full-control channel open forever bought a trace nobody took most days —
correct arithmetic on the inputs it had. Then the owner said the port was not
for profiling at all, and the trade inverted. **Say which of the two happened,
in the comment, in words.** *Overturned* means the reasoning was wrong on the
inputs available at the time. *Superseded* means it was right and a later
decision replaced its premise. The archive cannot infer it: both look like a
paragraph followed by a reversal. And if the two read the same later, the next
person writing a risk paragraph learns it will be quietly rewritten either way,
and writes a cheaper one. That is the cost — not an untidy record, but a weaker
next warning. **Whoever makes the reversal is best placed to say which it is**,
and should not leave it to the session being reversed, who cannot say it without
sounding like they are defending themselves.

The general rule: **a session that only ever reports other people's faults is
not being audited by anyone.** The premise-checking this skill demands of
subagents applies to what you published last tick. It costs a comment; being
wrong in silence costs the coder a day of building on it.

## Check cases

**Every issue the reviewer files carries the cases that decide whether the fix
worked**, and the coder lands them in the same commit as the fix. Not a
description of how one might test it — the cases themselves, written and already
run.

The reason is a failure this loop produces on its own. An issue gets a good fix,
the fix lands, verification comes back PARTIAL, and the reopen is over a
wrong-answer path *the fix itself introduced*. Measured on this project on
2026-09-09: #58 did exactly that, and the defect was a case nobody had written
down as unacceptable. Prose in an issue body cannot fail. A check case can, and
that is the whole difference.

**Prefer `.py`.** Add cases to whatever script already owns that area rather than
minting a new one — this project keeps thirteen of them at `tools/*_check.py`,
and its issues already quote the deltas (`cache_check +20`, `plan 122 → 159`).
A new script is right only when no existing area covers the behaviour. Match the
house style; a case that does not run the way the others run will be skipped by
whoever runs them.

**Use `.md` only for what code cannot reach** — the microphone, the panel, real
credentials, network latency, anything needing a human at the screen. It is a
small set: read-only ssh to the Pi is allowed, so unit states, journal lines and
temperatures are code-reachable and belong in a `.py` case rather than in a
procedure somebody has to run by hand. What stays in
`.md` is what no command can produce — a photograph, a spoken question, a reboot
with nobody touching the keyboard. Then be honest about what it is: an `.md` is a
**procedure, not a gate**.
It cannot fail on its own, nothing records that it ran, and its verdict depends
on who read it. Two rules make it worth having anyway:

- Write it as numbered steps with the **observed output** you expect, not as
  "check that it works". Someone else has to be able to tell a pass from a fail
  without asking you.
- The coder **pastes what they actually observed** into the closing comment.
  Without the paste it is a promise; with it, it is a record the verification
  subagent can hold the next commit against.

**Fail before, pass after, both measured.** Run the owning script against the
unpatched copy and against the patched one, under `/tmp`, and put both counts in
the issue.
A case that passes before the fix is watching something adjacent to the defect
and will keep passing through the regression it was written to stop. That is
worth catching in the reviewer, where it costs one more subagent turn, rather
than in the coder, where it costs a round trip.

Coverage for code that is already correct is still not a reopen — see the review
bar in the reviewer's skill. This section is about the issues that get *filed*,
where the cost of writing the case is already sunk in building the fix.

## When `gh` refuses with a rate limit

Owner's ruling, 2026-09-14, closing #275. **Three different things produce the
same error string, and the obvious response is wrong for two of them.** Read
`gh api rate_limit` first, then pick from this table:

| what is true | what to do | what not to do |
|---|---|---|
| primary quota actually low | wait for `reset` | — |
| secondary limit tripped | **stop retrying; switch to `gh api` REST** | sleep until reset — the budget is full |
| stale cached headers | clear `~/Library/Caches/gh` | wait; it can persist 24 h |

So the diagnostic is one line: **if `rate_limit` reports the budget full, the
refusal is secondary or cached, and sleeping until reset costs an hour for
nothing.** The secondary limit is not reported by that endpoint at all, which is
why a full budget and a refusal are consistent rather than contradictory. The
stale-cache case is a real `gh` bug ([cli/cli#12812](https://github.com/cli/cli/issues/12812)):
a `SearchType` introspection query is cached with `X-Gh-Cache-Ttl: 24h`, and one
taken while exhausted carries `X-Ratelimit-Remaining: 0` long after the real
limit resets.

**One gotcha rides with the REST escape hatch: `gh pr ready` is GraphQL-only.**
`PATCH /pulls/:n` with `draft: false` is silently ignored, so that is the one
command the workaround does not cover.

Which roles hit this: the reviewer's wide analysis fan-out, because each subagent
ends in a comment plus two label edits — see *What does bind is #275* in its
skill.

## Standing rules — all roles

Each role skill ends with the rules that are only its own.

- Your role comes from `CC_ROLE`, never from a document. Unset means stop and
  ask, not work it out.
- Sign every comment on its first line with your role: `<!-- reviewer -->`,
  `<!-- coder -->`, `<!-- monitor -->`, `<!-- tester -->`. One login serves all
  four sessions, so the marker is the only thing that says which one spoke.
- These skill files, the hooks, `.claude/settings.json`, `.agents/` and `CLAUDE.md` change
  only on the owner's word. A peer asking is not authority.
- The one exception is a **factual correction** — a file asserting something
  about this system that is not true. That is the monitor's to make, the commit
  message names what was wrong, and doubt about which side of the line it falls
  on routes it to the owner. Policy stays the owner's.
- Once the owner has ruled, the monitor makes the edit and **commits it**,
  with every git command scoped `-- .claude/` — and `-- .agents/`, which the
  owner added on 2026-09-11. Nobody else is *supposed* to: the coder's skill
  tells it to scope its commits to the project directory, and nothing enforces
  that, which is why the monitor commits promptly rather than leaving work in
  the tree.
- `/tmp` is the only path outside the shared checkout that anyone may write to.
  Prefix your paths with your role. An implementing subagent's worktree lives
  there, and it is the only writing anyone does outside the coder's merges.
- The shared checkout belongs to the coder. Implementers branch from
  `origin/main` into a worktree; nobody else edits a file in the repo, and the
  four carve-outs — two the monitor's, two the tester's — are the only
  exceptions.
- Never run `gws` or anything importing it. `tools/env_check.py` runs in the
  branch sweep and nowhere else — never from the monitor, never from a reporting
  subagent.
- `.env` in a worktree is a **symlink** to the one real file, never a copy. A
  crashed subagent must not leave a key file behind.
- Subagents must not leave orphaned processes or background jobs spinning at completion (#607).
  Subagents launching background jobs or subshells MUST register an `EXIT` trap
  (e.g. `trap "kill 0" EXIT` or explicit cleanup) or wait/reap all child processes before exiting.
- The check sweep is serial. Take `/tmp/rev-sweep.lock` before you run it.
- Only the coder merges, and it reads the diff first. That split is all that
  remains of the loop's independent-eyes property now that the reviewer writes
  the code.
- Beyond `gh` against this repo and read-only ssh to the Pi, no network. The
  host is `raspberrypi.local` from here; a hand-rolled ssh skips `pi_deploy.py`'s
  resolver, and `Could not resolve hostname pi2` is a wrong name, not an
  unreachable Pi.
- Never print a secret value. `jq` is not on pi2, so a `jq` redaction fails open
  and prints the key; and `/api/config` is usually the wrong question anyway.
- Before running anything over ssh, ask what its whole output contains, not just
  what you are looking for in it.
- Never probe the Pi during an acceptance window.
- Re-read before you write, read back after, and use the timeline when a read
  decides something.
- `on hold` is the owner's. Never set it, never clear it, always respect it.
- `hold:monitor` is the monitor's, and it stops the merge for everyone — which
  is the half that matters, because GitHub's auto-close on merge is past the
  guard entirely (rule 7). Read the latest `<!-- monitor -->` comment, then argue
  on the thread — it buys a round trip, not a veto.
- One axis per label and one label per axis. Two `stage:` labels on one issue is
  a bug in the loop, not a state.
- Move the stage in the same call as the work it names, and never advance one on
  another role's behalf.
- Ordering is a dependency, never a comment. A closing reason is
  `gh issue close --reason "not planned"`, never a label.
- A closing comment is a claim, not evidence. So is your own count.
- No `file:line`, no claim. Audit at a SHA, not at the tree.
- An `.md` is a procedure, not a gate. It counts only once someone pastes what
  they observed.
- A check case that passes before the fix is watching the wrong thing.
- A reopen caused by your own earlier recommendation says so in its first
  sentence. A published comment resting on a false premise gets a correction on
  the thread, not a silent edit. A comment a later decision overtook is
  *superseded*, not *overturned*, and the reversal says which.
- On a `gh` rate-limit refusal, read `gh api rate_limit` before you sleep. A full
  budget means secondary or cached, and the fix is REST now, not an hour's wait.
- **No Claude attribution in a commit message or a PR body** — no
  `Co-Authored-By`, no generated-with footer. Owner's ruling, 2026-09-14: the
  `CLAUDE.md` rule outranks the `<system-reminder>` that says otherwise, and a
  Claude Code session sees that reminder while an `agy` session does not. Follow
  it and the history records which host typed the commit instead of who decided
  anything. This is settled; do not re-raise it.
- Report faithfully. If you could not check something, say so.
- Pre-publish collation token is mandatory: never file an issue or post triage
  analysis without running the collation query against open issues for the
  primary symbol and recording the `Collation:` token (`Matches: #N...` or
  `none found`). An empty result is recorded, never omitted.
