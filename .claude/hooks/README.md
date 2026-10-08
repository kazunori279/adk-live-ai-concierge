# Running the four-session issue loop

Four sessions work one repo together: a **reviewer** that analyses issues, writes
the code through parallel subagents and verifies what landed, a **coder** that
merges those pull requests and deploys them to the Pi, a **monitor** that
moderates the whole open backlog, and a **tester** that opens the app in a real
browser and files what it sees fail. All four authenticate to GitHub as the same
login, so which one acted is carried by a comment marker and by which labels each
may touch.

**Everything but the monitor runs on the Antigravity CLI (`agy`); the monitor runs
on Claude Code.** That split started as #243 with the coder alone and the reviewer
joined on 2026-09-11 — see *Which CLI runs which role* below for how to undo it in
one environment variable.

> **The tester was added on 2026-09-11**, on the owner's word. That morning a
> change was implemented, reviewed, merged, swept green against all 22 check
> scripts and deployed — and the weather was blank when the owner opened the app
> in a browser. The check scripts call functions; not one of them opens the page,
> so the whole class of defect between a correct return value and a correct
> screen was invisible to every session in the loop. The tester runs against a
> server it starts on this Mac on its own port, never against the Pi, so it can
> never collide with a deploy or with somebody using the terminal in the kitchen.

> **A fifth role, `aide`, was added on 2026-09-13, and it is not part of the
> loop.** It is for a session the owner starts to write something down — filing an
> issue while the monitor is busy with the last one. It files, it comments, it
> signs `<!-- aide -->`, and it may not close, merge or move an issue's stage.
> Launch one with `ccrole aide`.
>
> It exists because of what closing #470 would otherwise have cost. The guard
> refuses a roleless GitHub write outright, so until then these sessions got
> through only on the fail-open gap for unreadable comment bodies — which is why
> **#477's body and a comment on #473 are in the timeline unsigned.** Closing the
> gap without the role would have refused the owner's own sessions with *"ask the
> owner which role this session is."*

> **The loop was restructured on 2026-09-11** (#282). The coder used to implement
> issues one at a time; now the reviewer's implementing subagents do that in
> parallel, each in its own `git worktree`, each ending in a pull request, and the
> coder merges and deploys and nothing else. Two things in this file follow from
> that and are easy to miss: **only the coder may run `gh pr merge`**, which the
> guard now enforces, and **`ccloop.sh` drives batches of pull requests rather
> than one issue per iteration.**

Both of the rules above are enforced by `role_guard.py`, a `PreToolUse` hook on the
shell tool. It reads the role from `CC_ROLE` — never from a document, because a
session that reads a skill and concludes it is the reviewer is indistinguishable
from one that was told to be. `ccrole.sh` is how the role gets into the environment.

`role_guard.py` refuses GitHub **writes** only. Reads are untouched, so a session
with no role can still investigate and report.

## Start a session in a role

One terminal per role, each started from the project directory (anywhere at or
below the git root works, so the hook path resolves). `ccrole.sh` adds the flags
itself, so the role name is the whole command:

- `--dangerously-skip-permissions`, for `agy` and `claude` alike, unless the
  caller already passed it (2026-09-25).
- `--add-dir <repo root>`, for both CLIs. `agy` needs it to load
  `.agents/hooks.json` at all (#643). The monitor on `claude` needs it to read and
  commit `.claude/`, which sits above the project directory.
- `--model gemini-3.8-flash-high` for `agy` roles (see *On the model*).

| Role | Start with | How it runs |
|---|---|---|
| wake | `ccwake.sh` | a non-LLM watcher that ends a role's backoff when its GitHub work changes (#698) |
| coder | `ccloop.sh coder` | unattended: a fresh `agy --print` session per batch, backing off while there is nothing to merge |
| reviewer | `ccloop.sh reviewer` | unattended: a fresh `agy --print` session per tick |
| tester | `ccloop.sh tester` | unattended since #667 (`e22105e`): a fresh `agy --print` session per tick, backing off on `LOOP-TESTER-IDLE` |
| monitor | `ccrole monitor`, then `/loop <instruction>` | interactive `claude`, pacing itself with `ScheduleWakeup` |
| aide | `ccrole aide` | interactive, outside the loop |

```bash
cd ${SHARED_REPO_PATH}
.claude/hooks/ccloop.sh coder
```

```bash
cd ${SHARED_REPO_PATH}
.claude/hooks/ccloop.sh reviewer
```

```bash
cd ${SHARED_REPO_PATH}
.claude/hooks/ccrole.sh tester
```

```bash
cd ${SHARED_REPO_PATH}
.claude/hooks/ccrole.sh monitor
```

### ccwake: start the next tick when work appears (#698)

Start it in its own terminal next to the three `ccloop.sh` roles. It does not start
itself, and the loop works without it, only slower:

```bash
.claude/hooks/ccwake.sh
```

It is not an LLM. Every `WAKE_POLL` seconds (45 by default) it makes one GraphQL
request (`ccwake_sig.py`) and computes each role's work signature:

| Role | Signature |
|---|---|
| coder | open PRs with their head SHAs |
| reviewer | issues at `stage:triage` and `stage:ready`; open PRs with head SHA and latest comment; the last 30 closed issues without `stage:verified` |
| tester | `main`'s SHA; `stage:live` issues with their latest comment |

`ccloop.sh` writes the role's signature to `$LOGDIR/signature` at the end of every
tick. `ccwake.sh` touches `$LOGDIR/wake` when the live value differs from that
record, and the countdown sees the file within a second, deletes it, resets the
backoff and starts the tick. Recording at the end of the tick is what keeps a role
from waking itself with its own writes. A role with no record yet is never woken,
and a failed poll never wakes anyone. `IDLE_GAP_MAX` is unchanged: it is now the
fallback heartbeat, and it can be raised once the wake has been observed working.

### Idle detection (#696)

A tick counts as idle only when the agent's final response has a line that is the
sentinel and nothing else. The raw stream also holds every tool result, and the
roles read their own skills, which quote their sentinels. Grepping the whole
stream read busy ticks as idle, as did a final message that named the sentinel to
say it was not printing it.

`ccloop.sh` launches each session through `ccrole.sh`, so the role, the flags and
the guard are the same as a hand-started session. It logs every iteration to
`/tmp/cod-loop/` or `/tmp/rev-loop/` as `NNN.log`, `NNN.jsonl` and `NNN.err`, and
the numbering restarts at `001` each time it is launched, overwriting the previous
run's files.

**None of these come back after the Mac reboots.** Auto-login restores the GUI
session, not the terminals, so all four have to be started again by hand. The
monitor does not need `--resume`: its state is `current_issues.md` and the GitHub
timeline, both of which survive.

`ccrole` is a shim in `~/.zshrc`. Where it is not defined (a non-login shell, a
script, another machine), call the script by path as above.

Everything after `--` goes to the CLI, so `--resume` or `--continue` go there:
`ccrole monitor -- --resume <session-id>`. `ccrole.sh` `exec`s the CLI, so the
session replaces the shell in that terminal, and it prints `coder session on agy`
to stderr first so the terminal records which one you got.

## Which CLI runs which role

```
$ ccrole cli
monitor    claude
reviewer   agy
coder      agy
tester     agy
aide       agy
```

`agy` is the Antigravity CLI. It took the coder in #243, and the reviewer and the
tester on 2026-09-11. The two CLIs share everything that matters — the guard, the skills, the
label scheme — and the sessions talk only through GitHub issues, so a monitor on one
and a reviewer on the other still work the same queue.

**Why the reviewer's move was checked before it was made.** The restructured loop
depends on three `agy` capabilities, and if any were missing the design would have
had nowhere to run. All three are present in the binary: **parallel subagents**
(`run_subagent`, `define_subagent`, `SubagentHub`), **git worktree checkouts**
(`CheckoutWorktree`, `delete_worktree_after_checkout`), and a **`schedule` tool**
taking either `DurationSeconds` for a one-shot timer or a 5-field `CronExpression`.
That last one is what replaces `/loop` and `ScheduleWakeup` for the reviewer.

**The monitor staying on Claude Code is a choice, not a leftover.** It is the only
role that writes files in this repo, its `/loop` dynamic pacing has no exact `agy`
analogue, and one session on each CLI is what keeps the comparison a comparison.

**Reverting takes no edit to any file**, because `agy` may simply be worse at this
than Claude Code and that has to be cheap to find out:

| | |
|---|---|
| `export CCROLE_CODER_CLI=claude` | put the coder back, permanently, in `~/.zshrc` |
| `export CCROLE_REVIEWER_CLI=claude` | put the reviewer back |
| `export CCROLE_TESTER_CLI=claude` | put the tester back |
| `CCROLE_CODER_CLI=claude ccrole coder` | put one session back, for that session |
| `CCROLE_CLI=claude ccrole <role>` | force one CLI for every role |
| `CCROLE_AGY_MODEL=<id> ccrole <role>` | pin a model for every `agy` role |
| `CCROLE_AGY_MODEL_REVIEWER=<id> ccrole reviewer` | pin a model for one role |

Nothing else has to move: the guard, skills and rules are all shared rather than
forked, and the sessions that stayed keep running.

**On the model.** #243 asked for `gemini-3.8-flash` at high reasoning effort, and
that is the default every `agy` role gets: `ccrole.sh` passes
`--model gemini-3.8-flash-high` unless overridden. It is pinned rather than left to
`agy`'s default because the trial is of a named model — *"the coder got slower"* has
to mean the CLI, not whichever model the CLI picked that week.

**The two `agy` roles stopped being comparable on 2026-09-11, which is why the pin
is now per-role.** The coder reads a diff and runs a deploy; the reviewer analyses,
writes the code through subagents and signs the verdicts. One model chosen for a
merge path is not automatically the right model for the other, and
`CCROLE_AGY_MODEL_REVIEWER` raises one without raising both.

**Every launch logs `failed to apply model override`, and every launch applies it
anyway.** `agy` consults its local config before the model list has arrived, does
not find the name, and logs the miss at `E` level with those words. Two hundred
milliseconds later the list lands, the name resolves, and the override goes
through. Both lines are in every `cli-*.log`; only the first one looks like an
outcome. Measured across three sessions on 2026-09-11 — this cost #267, filed
against the error line and closed once the success line was found. So grep for
the success, not the failure:

```bash
grep -o 'Propagating selected model override to backend: label="[^"]*"' \
  ~/.gemini/antigravity-cli/log/cli-<stamp>.log | sort -u
# -> Propagating selected model override to backend: label="Gemini 3.8 Flash (High)"
```

Absent means the pin genuinely did not land. **Do not try to read the model off
the requests** — `streamGenerateContent` goes to `models/gemini-3.8-flash` at
every effort level, so the wire says nothing about `-high` versus `-low`. That
line above is the only artefact that records the effort a past session ran at.

**`agy models` lies when you are not signed in.** Before authentication it answers
with a shorter list topping out at 3.6 Flash, and it answers confidently: no error,
no warning, the same `Fetching available models...` line. Signed in, the real list
starts at 3.8 Flash. That cost this repo a wrong measurement written into #243, so:
**run `agy models` only after `agy` has completed its browser sign-in**, and treat a
list with no 3.8 in it as evidence you are logged out rather than evidence about the
models.

### What each CLI reads

| | Claude Code | Antigravity (`agy`) |
|---|---|---|
| role | `CC_ROLE`, set by `ccrole.sh` | same |
| guard wiring | `.claude/settings.json` | `.agents/hooks.json` |
| guard event | `PreToolUse`, matcher `Bash` | `PreToolUse`, matcher `run_command` |
| triage-gate event | `Stop`, no matcher | `Stop`, no matcher |
| skills | `.claude/skills/` | `.agents/skills/*/SKILL.md` (symlinks to `.claude/skills/`) |
| rules | `CLAUDE.md` | `.agents/rules/AGENTS.md` (symlink to `global.md`); `project.md` symlinks `CLAUDE.md` |
| refusal word | `{"decision": "block"}` | `{"decision": "deny"}` |
| session id | `session_id` | `conversationId` |

### How agy and Claude Code share the config

Everything in the right-hand column is a pointer at the left-hand column's file, not
a copy of it. Two copies of a 752-line skill would drift within a week, and the
trial is only worth running if both sessions are under the same rules.

- **Skills:** `agy` discovers skills natively by traversing the directory convention at
  `.agents/skills/*/SKILL.md`. In CLI mode, `entries` in `.agents/skills.json` did not
  resolve or register loose directories (#280). To share the skills without duplication,
  tracked relative symlinks are committed under `.agents/skills/` pointing to
  `../../.claude/skills/` (`issue-looping-coder`, `issue-looping-reviewer`,
  `issue-looping-monitor`, `issue-looping-shared`, `issue-looping-tester`) and
  `../../2026-0824-no-ai-slop/no-ai-slop` (`no-ai-slop`). `agy` follows these symlinks
  and registers all loop skills in `<skills>` at startup.
- **Rules:** `agy` discovers rules by looking for `AGENTS.md` (or `GEMINI.md`) walking
  upwards. `.agents/rules/AGENTS.md` is committed as a tracked relative symlink to
  `global.md`, ensuring `<user_rules>` is populated without manual copying.

`role_guard.py` reads both payload shapes and picks the refusal word from whichever
arrived, so there is one guard rather than two. The session id matters for
`ccrole.sh assign`: under `agy` the id to pass is the `conversationId`, not a Claude
Code session id.

### The second hook: `triage_gate.py`

Added 2026-09-12, on the owner's word. A `Stop` hook that keeps the **reviewer**
from ending a tick while more than three unblocked P0/P1 issues sit at
`stage:triage`; the refusal names them and tells it to fan out one analysis
subagent per issue, in one message. Every other role passes straight through.

It exists because the reviewer's skill already said all of this, at length, and
the reviewer still ran a pipeline of width one. Measured that morning: six issues
filed between 07:05Z and 07:30Z, five unblocked P1, zero reviewer comments on any
of them at 07:30Z, and the day's whole output one analysis plus one pull request.
Three of the gate's four triggers were firing the entire time. The same failure
had been self-diagnosed and answered with another paragraph three times already,
which is what *Why it exists* in the shared skill predicts: making the convention
louder inherits the defect.

Three things differ from the guard and all of them matter:

- **The refusal word is the same on both hosts.** `agy`'s `Stop` enum is `stop` /
  `continue` / `block` and Claude Code's refusal is `block`, so unlike
  `PreToolUse` there is no `block`-versus-`deny` branch. The *allow* word still
  branches: Claude Code takes `{}`, and `agy` needs an explicit `{"decision":
  "stop"}` — an empty object there reads as a refusal, which on a `Stop` hook
  would mean a session that can never end its turn.
- **It can stand down.** Three consecutive refusals for one session and it allows
  the stop, plus a 45-second cooldown so one turn cannot bounce. A queue that
  genuinely cannot be drained becomes the monitor's problem rather than a wedge.
- **It stands aside for subagents.** Claude Code raises `SubagentStop` for a child
  and so never presents one here; `agy` registers `Stop` and no subagent variant,
  so the hook checks for `parentConversationId`, `subagentName`, `agentType` and
  `mainConversationId` and passes through on any of them. Without that, the first
  fan-out the gate ever produced would have had every one of its own children
  blocked by the gate that asked for them.

Like the guard it fails open on everything — no role, no `gh`, a timeout, a bad
payload, any unhandled exception — and prints the reason to stderr.

### First prompt

The role skills dispatch on `CC_ROLE`, so the session finds its own file:

| Role | First prompt |
|---|---|
| coder | `Use the issue-looping-shared skill and start working the queue.` — or just run `ccloop` |
| reviewer | `Use the issue-looping-shared skill and start working the queue.` |
| monitor | `Use the issue-looping-shared skill, then /loop` |
| tester | `Use the issue-looping-shared skill and start running the scenarios.` |

**`/loop` with no interval** is the monitor's, and only the monitor's now. It is
dynamic mode, where the session paces itself with `ScheduleWakeup` rather than
firing on a fixed timer, and the monitor's skill is written for it.

**No `agy` role gets `/loop`, and they solve it differently.** `agy --help`
offers `--print` (one prompt, non-interactively, then exit), `--continue`, `-i` and
a `remote-control` daemon, and nothing that keeps working; `/loop` is a Claude Code
bundled skill, so there is no file for `agy` to find.

- **The reviewer paces itself**, with `agy`'s own `schedule` tool:
  `schedule(DurationSeconds=270, Prompt="<the original instruction>",
  TimerCondition="any")`, re-armed at the end of every tick. `TimerCondition="any"`
  wakes it early the moment a subagent finishes, which is most of what it waits
  for. It stays **one long session** on purpose: its triage gate counts *"across
  three of your ticks"*, and a fresh session per tick cannot count that.
- **The coder gets a shell wrapper**, because its unit of work carries nothing
  forward and a fresh session per batch re-reads the skills from disk.
- **The tester uses `schedule` as well**, but on a much longer timer and keyed to
  a commit rather than to a clock: it polls `origin/main`, waits for the SHA to
  stop moving, and then runs the whole scenario set. Twenty to thirty minutes
  between polls, because a sweep and a merge together take longer than that.

**The coder does not work back to back on its own, which this file claimed until
2026-09-11.** It finishes and stops: measured that day, launched 10:04:43, closed
#246 at 10:12, then sat idle. Either drive it by hand, or use the wrapper:

```bash
ccloop           # merge and deploy batches until the queue stays empty
MAX=3 ccloop     # stop after three batches
```

`ccloop.sh` sits next to `ccrole.sh` and calls it, so the role and the model pin
come from one place. It starts a **fresh session per batch** rather than
`--continue`-ing: nothing learned merging one batch is needed for the next, and a
fresh session re-reads `.claude/skills` from disk, which closes #260 for the coder
without anyone having to remember to restart it.

**An empty queue no longer stops the loop.** The coder used to pick its own issues,
so nothing startable meant nothing to do; now the work arrives asynchronously from
the reviewer's subagents, and an empty queue usually means a fan-out is still
running. So the coder prints `LOOP-NOTHING-TO-MERGE`, the wrapper waits `IDLE_GAP`
(180 s) and tries again, and only `IDLE_MAX` (5) empty iterations in a row stop it.
`MAX`, `GAP`, `TIMEOUT` and `LOGDIR` are the other knobs; `TIMEOUT` defaults to 30m
because `agy`'s own `--print-timeout` default is 5m.

Add the short form to `~/.zshrc` beside `ccrole`:

```bash
ccloop() { "${SHARED_REPO_PATH}/.claude/hooks/ccloop.sh" "$@"; }
```

## Confirm it took

In the new session:

```bash
echo "${CC_ROLE:-<unset>}"
```

To prove the guard is also *wired* — a separate thing from the role being set — have
the session run:

```bash
gh issue comment 99999 --body 'wiring test'
```

The guard should refuse it and name your role: *"Role is `coder`, so this comment
must be signed `<!-- coder -->`"*. Anything else means the hook is not running: a
GraphQL 404 means the command reached GitHub, and a complaint about having no role
means `CC_ROLE` did not survive. Issue 99999 does not exist, so nothing is posted
either way.

**Run this in the `agy` coder session too, and do not skip it.** The wiring on that
side has never been exercised: `.agents/hooks.json` was written from the CLI's own
embedded documentation, not from a session that watched it fire, and a hook config
in a place the CLI does not look is indistinguishable from no guard at all. The
worst version of this is the quiet one, which is exactly the shape of the #155
incident that produced the guard. Two specific things to watch on the first run:

- **The refusal should say `deny`, not `block`.** If it says `block`, the payload
  fell through to the Claude Code branch and the label rules may be reading an empty
  command.
- **Allowed calls used to return `{}` with no `decision` field, and on `agy` that
  denied everything.** The reasoning was that Antigravity documents `decision` as
  required on `PreToolUse`, but its values are all opinions — `allow`, `deny`,
  `ask`, `force_ask` and `deny_unless_prior_grant`, five rather than the four
  written here until 2026-09-11 (#273) — and none of them means *no opinion*, so
  the guard stayed silent rather than auto-approving every shell call. **Measured
  2026-09-11, the first time the `agy` side was ever exercised: `agy` reads the
  empty object as a refusal, so the coder was blocked on its own first command.**
  The paragraph above anticipated this — *"if `agy` complains about the empty
  object on every command, that is the thing to fix"* — and named `ask` as the
  fallback; what shipped is `allow`, gated on `DENY == "deny"` so the Claude Code
  branch still answers `{}`. **The coder made that edit itself at 10:10, to the
  guard that guards it**, which is not its file; it is recorded on #260's
  neighbours rather than reverted, because reverting re-blocks every command it
  runs. The permission question — `allow` auto-approves, and only
  `--dangerously-skip-permissions` makes that a no-op — is the owner's.

## What needs a restart

| Changed | Restart? |
|---|---|
| `.claude/settings.json` (which hooks are registered) | **yes** |
| `.agents/hooks.json`, `.agents/skills.json` | **yes** |
| A skill under `.claude/skills/` | **yes** |
| `role_guard.py`, `triage_gate.py` or `ccrole.sh` themselves | no — re-read on every invocation |

The first row is the one that bites, because it fails silently. A session started
before a hook was registered never runs it, and the guard is simply absent for that
session — no warning, no error, writes go straight through. Verified on 2026-09-10:
the hook was registered at 10:58, an unsigned `gh issue comment` from a session
started earlier reached GitHub untouched, and the identical command from a session
started afterwards was refused.

`--dangerously-skip-permissions` does **not** disable hooks. It bypasses permission
prompts; `role_guard.py` still runs and still blocks. Confirmed the same day.

## Why not `/clear`

`/clear` cannot put a session into a role. `CC_ROLE` is a process environment
variable, fixed when `claude` launched, and nothing inside the session can set it.

`ccrole.sh assign` writes a file keyed by session id as a fallback, and that is what
`/clear` puts at risk: if the session id changes, the assignment no longer matches
and every GitHub write is refused. Relaunching through `ccrole.sh` sets the
environment variable, which outranks the file and does not depend on the id.

Relaunching also needs no `/clear` — it is a new process with an empty context.

## Give a role to a session already running

For a session mid-task, where relaunching would throw away context that cost real
time to build:

```bash
ccrole assign <session-id> coder
```

The session id is in the guard's refusal message, so the usual path is: the session
tries to write, gets refused, quotes its id, you assign. This does not survive a
restart, and it does not help a session that predates the hook — that session never
calls the guard at all, so it never reads the file.

## Housekeeping

```bash
ccrole list
ccrole revoke <session-id>
```

Assignments outlive the sessions they were made for, so `list` accumulates dead
session ids. They are inert — a dead id matches nothing — but clear them out so the
list stays readable.

## When the guard refuses you

The refusal says which rule you hit and what to do about it. Seven cases:

- **No role.** Ask which role the session is. Do not pick one; that is the failure
  this exists to stop.
- **Unsigned comment.** Add your marker as the first line: `<!-- coder -->`,
  `<!-- reviewer -->`, `<!-- monitor -->`, `<!-- tester -->`, `<!-- aide -->`. It is checked in the command line and in
  any `--body-file`.
- **Someone else's label.** `stage:ready` and `stage:verified` are the reviewer's,
  `stage:pr` is applied by the reviewer and cleared by the coder at merge,
  `P0`/`P1`/`P2` are the reviewer's and the monitor's to move, and the tester's to
  set at filing and never move after, and `on hold` is the owner's
  alone. `stage:owner` is deliberately not in `LABEL_RULES`: the reviewer, coder
  and monitor may all set and clear it, and the guard passes unlisted labels through via
  `LABEL_RULES.get()`. It means *waiting on the owner*; `on hold` means *the owner
  said stop*.
- **Merging when you are not the coder.** `gh pr merge`, and `PUT /pulls/N/merge`
  under `gh api`, are the coder's alone. The reviewer writes the code through its
  subagents, so letting it merge too would put authoring and landing in one session
  and leave nobody reading the diff cold. Add `stage:pr` and let the coder take it.
- **A comment body the guard cannot read.** Since 2026-09-13 (#470), `--input -`,
  `-F body=@-`, a stdin pipe, a heredoc fed straight to `gh`, and a comment with
  no body flag at all are refused rather than waved through. Write the body to
  `/tmp/<role>-<N>-body.md` and pass `--body-file`. **Writing that file with a
  heredoc in the same command is fine** — the guard reads the file, not the
  redirection that produced it.
- **Closing as the tester or the aide.** Neither sees the queue, so neither
  decides an issue is done. Say it on the thread and leave the state alone.
- **The issue carries `hold:monitor`.** Since 2026-09-13 (#490) the monitor can
  put that label on to say *not this patch*, and while it is there the issue does
  not close and a pull request whose `Closes #N` names it does not merge — for
  every role, the monitor included. Read the newest `<!-- monitor -->` comment on
  the thread: it has to say what is contested and what would lift it. It is a
  round trip, not a veto; argue if you think it is wrong. The monitor lifts it
  with `--remove-label hold:monitor`, and may do that in the same command as the
  close.

  This is the only rule that queries GitHub, so it runs last and only on a close
  or a merge that everything else already allowed — 0.55 s on those, unchanged on
  everything else — and it fails open when `gh` cannot answer.

  It exists because the prose version demonstrably does not work: on 2026-09-12
  the monitor asked for a hold on #476 in a comment at 21:41Z, the pull request
  was opened at 21:45Z, merged at 21:53Z and deployed at 21:54Z. The coder polls
  labels and `gh pr list`; a hold that is neither is invisible.

Label refusals see REST as well as `gh issue edit`: both
`gh api -X POST .../labels -f 'labels[]=on hold'` and
`gh api -X DELETE .../labels/stage%3Aready` are checked, the second after
percent-decoding. The colon-encoded form is not exotic — it is the documented way to
delete a label whose name contains a colon, so every session that has cleared a
stage label the hard way has typed it.

The guard fails open on its own bugs — an unusual quoting style that crashes it
allows the command rather than blocking every GitHub write in the loop. It is not a
security boundary; a session that wanted a role could export `CC_ROLE` itself. It
defends against a session that never considered the question, which is every case
observed so far.

## See also

- [`../skills/issue-looping-shared/SKILL.md`](../skills/issue-looping-shared/SKILL.md) —
  what the roles are, the label scheme, and the rules that bind all of them
- `role_guard.py` header — the incidents that produced this
