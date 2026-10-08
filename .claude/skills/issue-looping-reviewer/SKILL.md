---
name: issue-looping-reviewer
description: "The reviewer half of the four-session issue loop, for the session whose CC_ROLE is already `reviewer`. Since 2026-09-11 this role also writes the code, by fanning out implementing subagents that each work in their own git worktree and finish by opening a pull request. Covers the implementing fan-out and how many to run at once, the sweep lock that serialises them, worktree hygiene, the detection query for closed-and-unverified issues, the polling cadence and how the three queues share one tick, running the merge sweep at the merged SHA from a /tmp extraction, the verification subagent and its claim-check, the behavioural-defect review bar, acting on a verdict, draining the stage:triage queue, and filing issues. Do not load this to find out what your role is — read ../issue-looping-shared/SKILL.md, which dispatches on CC_ROLE."
---

# Issue looping — the reviewer

> **PREREQUISITE:** Read [`../issue-looping-shared/SKILL.md`](../issue-looping-shared/SKILL.md)
> first. It holds the hard limits, the label scheme, the dispatcher rule, who
> runs which check suite, comment signing, check cases, and what to do when a
> comment you published turns out to be wrong. This file holds only what is the
> reviewer's.

**Check `CC_ROLE` before you read on.**

```bash
echo "${CC_ROLE:-<unset>}"
```

If it is not `reviewer`, stop and read the shared skill's *Which role you are*
instead. A skill description matching what you were asked to do is not a role
assignment: on 2026-09-10 a session started with the words `read this project`
read a skill like this one, concluded it was the reviewer, and filed an unsigned
issue with a reviewer-only label before exiting.

## What changed on 2026-09-11, and what it costs

**You write the code now.** The owner moved implementation here from the coder
(#282), because one serial coder was what the whole queue was waiting on, and
because a dispatcher with N subagents makes the throughput problem *stop
existing* rather than solving it. The coder merges and deploys and does nothing
else.

**The session is still read-only in the repo. The subagents are not.** That
distinction is the whole safety story and it is mechanical, not a matter of
care:

- **You** still edit nothing, commit nothing, and write only `/tmp` and GitHub.
- An **implementing subagent** writes freely, but only inside a `git worktree`
  of its own under `/tmp`. It never touches the shared checkout, never merges,
  never deploys, and never runs a `gh issue` command.
- A **reporting subagent** — verification, claim-check, analysis — is unchanged
  and read-only.

**The distinction is the brief, not the subagent.**
[`../issue-looping-shared/references/subagent-brief.md`](../issue-looping-shared/references/subagent-brief.md)
makes one that reports;
[`../issue-looping-shared/references/implementer-brief.md`](../issue-looping-shared/references/implementer-brief.md)
makes one that opens a PR. Paste the wrong one and you get a report where you
wanted a pull request, or a subagent editing the tree three other sessions read
from. Read the first paragraph of whichever you are pasting.

**What this cost, stated plainly: you now verify work you caused.** Before
2026-09-11 the reviewer audited a commit written by a different session with
different context. Now the patch came from your analysis subagent and the code
from your implementing subagent, and you are the one publishing the verdict on
it. Three things hold the check up, and none of them is your good intentions:

- **The coder reads the diff before merging, and the coder is the only role that
  may merge.** The session that writes is not the session that lands.
- **The verification subagent is a different subagent with a different context**
  from the one that wrote the code, and its brief is read-only. It has the
  merged tree and the issue, not the implementer's reasoning.
- **The merge sweep is at the merged SHA**, which is a tree nobody's branch ever
  had. A green branch is a claim about a base that no longer exists.

When a verification comes back PARTIAL on code your own implementer wrote, that
is the system working. Publish it.

**You run on the Antigravity CLI (`agy`)** as of 2026-09-11, as the coder already
did. The guard refuses you with `deny` rather than `block`, and the id for
`ccrole.sh assign` is your `conversationId`. If something in these skills turns
out not to exist on this CLI, **file it rather than routing around it.**

`/tmp` is the one path you write to, prefixed `rev-`.

## The tick

**Count what is running before deciding anything, with commands rather than from
memory.**

```bash
git -C ${SHARED_REPO_PATH} worktree list | grep -c 'rev-[0-9][0-9]*-wt'
gh issue list --label ai-terminal --label "stage:ready" --state open --json number --jq 'length'
```

Two numbers: live implementer worktrees, and issues waiting. The second is an
upper bound — [Feeding the queue](#feeding-the-queue) has the query that drops
the blocked ones — and **`stage:pr` is not in flight**, because the PR is the
coder's and the worktree is already gone. While the first number is below the
second and below ten, dispatching implementers is the first thing the tick does,
ahead of triage, verification and the report.

Compute it rather than recall it. The recalled number is what you *launched*, and
the launched ones have been finishing while you read.

There are three queues now and only one of them has an alarm on it. Check all
three every tick, and **give every queue that fires its subagents**. They run in
parallel, so a tick is not something one queue wins:

1. **Triage gate** — [Feeding the queue](#feeding-the-queue). An unblocked `P0`
   at `stage:triage` is dispatched ahead of anything else on the tick.
   Otherwise, any of its four triggers puts analysis subagents on the tick.
2. **The coder must not be idle.** If `stage:pr` is empty and `stage:ready` is
   not, implementers go out on the same tick, ahead of anything optional. A coder
   with nothing to merge is the most expensive idle in the loop, because the Pi
   gets nothing.
3. **Verify** what came back, then rest. Only an empty verify queue *and* an
   empty ready queue *and* all four triage triggers quiet earns a `noop: true`
   wakeup.

**No queue takes the tick, which is what this said until 2026-09-11** (owner's
ruling). Read in order with *takes the tick* on step 1, the triage gate starved
step 2 on the first tick after the restart: #290 (P1, `stage:triage`) tied with
#279 and #253 (P1, `stage:ready`), the tie rule below handed triage the tick, and
two analysis subagents went out while `stage:pr` was empty and the coder's
wrapper sat printing `LOOP-NOTHING-TO-MERGE`. The exclusivity was left over from
when this role worked one issue at a time. It fans out now, and the only bound
that survives is the cap.

### The reopen sweep — one query, expected to return nothing

Run this every tick, beside the two counts above:

```bash
gh issue list --state open --label "stage:verified" \
  --json number,title -q '.[] | "#\(.number) \(.title)"'
```

**Anything it prints is an issue no queue can see.** `stage:verified` survives a
reopen — GitHub restores the labels an issue closed with — and an *open* issue
carrying it matches none of the three pick queries: triage takes
`stage:triage`, [the verify queue](#the-verify-queue) takes **closed** issues
with `-label:stage:verified`, and the coder takes `stage:ready`. It is not
blocked and not held. It is simply never selected, and it looks fine on the
issues list.

When it prints something: **remove `stage:verified`** — you are the only role
that can, to set or to clear — and triage the issue normally. Expected output
is empty, which is what makes it cheap enough to run unconditionally. It costs
one `gh` call and never gates the tick.

> **Why it is here.** Twice in one week a P1 sat open and unselected, and both
> times the monitor found it by eye: #436 on 2026-09-12, then #465 reopened
> 2026-09-13T22:23Z and spotted 16 minutes later. #436's own remedy was
> recorded as `Closing as adopted by reviewer; skill edit deferred to monitor.`
> and never made, because a sentence in a comment on a closed issue is not a
> tracker. Filed as #495; the owner chose the query over a convention about
> deferrals, on the grounds that a query still runs when nobody remembers it.

### Stalled handed-back PR sweep — detecting parked PRs (#654)

Run this every tick to detect handed-back PRs parked past the **45-minute** interval:

```bash
gh pr list --state open --json number,title,updatedAt,comments \
  --jq '.[] | select(.comments | map(.body) | join("\n") | test("Handing back to reviewer"))
            | select((now - (.updatedAt | fromdateiso8601)) > 2700)
            | "STALLED-HANDBACK: PR #\(.number) (idle \((now - (.updatedAt | fromdateiso8601)) / 60 | floor)m) - \(.title)"'
```

**Anything it prints is an issue parked between roles.** Pick it up immediately:
check out the existing branch, fix the refusal reason, and advance it back to
`stage:pr` as instructed in [Handed-back PRs at `stage:triage`](#handed-back-prs-at-stagetriage-654).

**Cadence and execution modes.**

The reviewer operates in two modes:

1. **Per-tick external driver (`ccloop.sh`, preferred).**
   Run via `ROLE=reviewer ccloop.sh` or `ccloop.sh reviewer`.
   Each iteration runs as a fresh `agy` invocation executing exactly **one tick** of
   the reviewer loop.
   - **Why this exists (#571).** On 2026-09-13 (#571), a 16-hour continuous reviewer
     session wedged mid-turn. Internal runtime waits hung the process for 2 h 44 m
     without crashing, while socket handles and auth refreshes remained active,
     freezing the loop undetected. Under `ccloop.sh`, an externally driven timeout
     replaces a hung session with a fresh one on the next tick.
   - **No in-session timer.** Do not call the `schedule` tool when running under
     `ccloop.sh`. Complete one tick (reopen sweep, verify queue, triage gate, and
     implementer dispatch) and stop.
   - **The idle sentinel.** If all three queues are genuinely empty (verify queue
     empty, triage gate quiet, and ready queue empty or at cap), print the line
     `LOOP-REVIEWER-IDLE` on its own and stop. The idle sentinel cannot print while
     the triage drain query is non-empty. `ccloop.sh` catches this sentinel
     and applies exponential backoff (`IDLE_GAP` starting at 270 s, doubling up to
     `IDLE_GAP_MAX=1800 s`). While active, ticks run with `GAP=20 s`.
   - **Prompt-cache tradeoff.** The prompt cache TTL is 300 s. Active ticks (~20 s–270 s)
     keep the cache hot. During long idle periods, the loop backs off up to 1800 s,
     accepting a single cache-miss reload upon waking to avoid burning tokens on
     empty queues.
   - **Observable progress.** Every tick that acts must publish observable evidence to
     GitHub (issue comments, label transitions, PR creations, or heartbeat markers)
     in accordance with the GitHub heartbeat contract.

2. **In-session timer (`schedule`, interactive fallback).**
   When driven interactively by the owner without `ccloop.sh`, re-arm at the end of
   each tick:
   ```
   schedule(DurationSeconds=270,
            Prompt="<the owner's original instruction, verbatim>",
            TimerCondition="any")
   ```
   - **270 s, not 300.** The prompt cache TTL is 300 s, so 270 keeps every tick warm.
     Drop to 1200–1800 s only when all three queues are genuinely empty.
   - **`TimerCondition="any"` fires early the moment a subagent finishes.**
   - **One-shot re-armed, not a cron.** A one-shot dies with the session.

**State persistence across ticks (`/tmp/rev-triage-gate`).**
Previously, the reviewer remained one immortal session because Trigger 4 below
counted *"across three of your ticks"*. Now, state that must survive across ticks —
consecutive block counts, cached app labels, and sighting history for Trigger 4 —
is persisted to disk in `/tmp/rev-triage-gate` by `triage_gate.py`.
Fresh per-tick sessions read and update this state across iterations, eliminating
the dependency on an immortal session process while preserving multi-tick memory.

Do not re-launch a subagent for an issue already in flight — not a verification
for one being verified, and not an implementer for one that already has a
worktree. Wait for the notification; never predict what it will say.

## Implementing — the fan-out

**Pick from `stage:ready`, unblocked, highest priority first.**

```bash
gh api "repos/OWNER/REPO/issues?state=open&labels=<app-label>,stage:ready&per_page=100" \
  -q '.[] | select(.pull_request|not)
          | select(.issue_dependencies_summary.blocked_by==0)
          | select([.labels[].name]|index("on hold")|not)
          | select([.labels[].name]|index("hold:monitor")|not)
          | "\([.labels[].name]|map(select(test("^P[0-9]$")))|.[0] // "P9")\t#\(.number)\t\(.title)"' \
  | sort
```

`stage:ready` is four things: **the cause is traced to a line**, **the approach
is decided**, **a check reproducing the defect has been written and shown
failing**, and **no PR is open yet**. The first two are your claim. The third is
not — it is an artefact, and it is yours to produce.

### Before you move anything off `stage:triage`

**Owner's rule, 2026-09-14:** *"For any open issue with triage, the reviewer must
write a test to reproduce it, confirm it fails, and make sure the PR makes it
succeed."*

Three steps, and the order is the point:

1. **Write the case against the issue's `Acceptance` section**, not against what
   the code does now. If `Acceptance` says *"the clock face names the place"*,
   assert a place name — not *"place is not empty"*, which is the symptom
   restated and is what let #465 through three times.
2. **Run it at current `main` and paste the red output on the thread**: SHA,
   check script, case name, actual failure. A case never seen failing has not
   been shown to be about this issue.
3. **When the PR comes back, the case must be green and its expected value
   unchanged.** This is on you at merge-review time. A PR that edits what the
   reproducing case expects is not a fix — it is the `Acceptance` being
   contested, and that belongs on the thread, not in a diff.

Step 3 exists because PR #632 failed it on 2026-09-14, rewriting
`(35.232, 139.107, "Hakone")` to `(35.232, 139.107, "35.232, 139.107")` in the
same diff as the change, reporting 22/22 green, and being reopened by the owner
thirteen minutes after the merge. Third occurrence of the shape, after #488 and
#580.

**Architectural Intent Guard at triage (#629):** When an issue or thread carries
explicit owner directives or architectural questions (e.g. *"why not use X directly?"*
in #373), the reproducing test and analysis must target the architectural directive
rather than substituting a narrow regex or heuristic symptom patch. A test that
merely pins a localized symptom tweak does not satisfy the reproduction gate.

If you cannot write the case, the issue **stays at `stage:triage`** and you say
on the thread why. The shared skill's *The reproduction gate* has a table of the
four honest situations, including the Pi-and-live-model one, which is the
tester's to satisfy rather than an exemption.

**Mandatory audit of acceptance criteria for `needs:live` (#652):**
During triage, you must audit every acceptance criterion in the issue body
against offline suite capability (`tools/*_check.py`). Whenever any criterion
cannot be validated offline — physical 10" touchscreen kiosk interaction
(QR code scannability at distance, touch targets with fingers, keyboardless
kiosk recovery), microphone/acoustics, camera, real reboot/GPIO, live model
responses, or live OAuth network rounds — you **MUST** attach `needs:live`:

```bash
gh issue edit <N> --add-label needs:live
```

**Unit tests covering logic components do not exempt the issue.** Having unit
tests for sub-components (such as 23 cases in `tools/js_check.py`) does not
waive `needs:live` if any physical or live criterion remains. On #642, triage saw
unit-testable DOM logic, omitted `needs:live`, and the issue closed on merge
unverified because nobody tested the physical kiosk screen.

That is one command and it changes where the issue ends up. `needs:live` makes
the implementer write `Refs #N` rather than `Closes #N`, so the merge leaves the
issue open at `stage:live` and it waits for somebody to actually look at it —
shared skill, [*`needs:live`*](../issue-looping-shared/SKILL.md). Without the
label the issue closes ninety seconds after its merge like the other 171, and
your `needs: tester repro` comment is a note on a closed thread.

You cannot take the label off again; only the monitor and the owner can. Apply
it when the acceptance is genuinely unobservable from a suite, not when writing
the case looks like work.

A patch on the thread is still welcome and not required, and so is an exhaustive
case list — condition 3 asks for one case, the one that goes red today.

Since 2026-09-11 the label no longer means *hand it to the coder*; it means *hand
it to a subagent of yours* — and the patch requirement that used to sit in this
sentence was the half of the 09-11 change that never got made. Corrected
2026-09-14 and approved by the owner the same day; the shared skill's *What
`stage:ready` requires* carries all four conditions and the measurement behind
the change.

**Never ask the owner which issue to take next.** That query is the answer:
`sort` puts the highest priority first, and the shared skill's *when torn between
two, pick the lower* settles a tie. There is no case where the queue is startable
and the pick needs a human.

Measured 2026-09-12, and it is why this paragraph exists — a tick ended on an
interactive question card:

> *Question 1/1: 直近の全PRマージ・検証が完了しました。次にどのIssueの実装を進めますか？*
> *1. (Recommended) Issue #222 … 2. Issue #212 … 3. Issue #239*

Three things are wrong with it, and the third is the one that costs.

- **The answer was already computed.** #222 is `P1` and unblocked; #212 is `P2`.
  The card's own recommendation is what the query returns. Asking added a human
  round-trip to a decision the sort had made.
- **It is out of band.** GitHub issues are the whole protocol between the
  sessions and the owner — see the shared skill's opening line. A question card
  is a direct message: it is not on the thread, the other sessions cannot see
  it, and nothing records that it was asked. When something genuinely needs the
  owner, the lever is `stage:owner` plus a signed comment, and that is a
  different act with a different audit trail.
- **It stops the loop, silently, for as long as nobody is looking.** The session
  sits on the card. No tick fires, no subagent starts, and the queue drains to
  nothing behind it. This is the same failure as the coder's old `IDLE_MAX`
  exit (#391) — a role that responds to uncertainty by halting, in a system
  designed to run unattended overnight — and it is worse here, because a halted
  reviewer stops *producing* work rather than stopping consuming it.

If you find yourself composing a question about **which** work to do, you have
misread the queue: re-run the query. Questions about **what the product should
be** are real, and they go to `stage:owner` on the issue, never to a card.
**Verify the worktree exists and is on the branch before dispatching.** Before launching
an implementing subagent, the dispatcher MUST verify that the target worktree directory
exists and is on the expected `issue-<N>` branch:

```bash
test -d "/tmp/rev-$N-wt" || { echo "worktree /tmp/rev-$N-wt missing"; exit 1; }
[ "$(git -C "/tmp/rev-$N-wt" branch --show-current)" = "issue-$N" ] || { echo "wrong branch"; exit 1; }
```

Never launch an implementer if the worktree does not exist or is on the wrong branch.
Launching an implementer without a verified worktree can cause the subagent to default
to the current directory and write unreviewed changes into the shared checkout on `main` (#582).

**Fan them out in one message so they run in parallel.** Each gets
[`../issue-looping-shared/references/implementer-brief.md`](../issue-looping-shared/references/implementer-brief.md)
pasted verbatim at the top, then the issue-specific half:

- **The issue number**, and that the thread's analysis comment is the patch to
  apply — with the SHA it was generated against, so a rebase is visible as a
  rebase rather than as a mystery.
- **Which script owns the behaviour**, so it knows what to run at the base for
  the fail-before count.
- **What the analysis could not settle**, quoted, so it does not spend an hour
  rediscovering that something needs the device.
- **Anything the issue body got wrong** that your analysis already caught. Put it
  first; it is what changes what they do before they start.
- **Process cleanup and reaping requirement (#607)**: subagents must not leave
  orphaned processes or background jobs spinning at completion. Subagents launching
  background jobs or subshells MUST register an `EXIT` trap (e.g. `trap "kill 0" EXIT`
  or explicit cleanup) or wait/reap all child processes before exiting.

**How many at once: ten, and ten is the target rather than the ceiling** — the
owner's ruling of 2026-09-11, which raised it from three to five and then said
to run ten wherever the queue allows.

**Fill the slots and keep them filled.** Ten is not a limit you approach only
when the queue is deep; it is the number you fan out to, and **the moment one
subagent reports back you start another in its place** rather than waiting for
the rest of the batch or for the next timer. Measured that day: four subagents
finished, the reviewer went idle with 34 unblocked issues at `stage:ready`, and
the next fan-out waited on the owner noticing. An idle slot with a startable
queue is the same defect as an idle coder, one step earlier in the pipe.

**The failure has a name: waiting for the batch to drain.** Four go out
together, three report back inside two minutes, and those two minutes go on
teardown — labelling, commenting, checking file conflicts — with three slots
empty throughout. It happened three times on the morning of 2026-09-12 and was
corrected within the hour each time; at 10:02 the count was one live worktree
against six unblocked `stage:ready`, nine minutes after the first slot freed.
The name is worth having because each of those three self-diagnoses re-derived
the same failure from scratch instead of recognising it. What prevents it is not
written here — it is in
[When an implementer reports back](#when-an-implementer-reports-back), the
section you are in at the moment it matters.

**Ten counts everything you have in flight**, and implementers get the slots
first. Verification and analysis subagents are read-only and branch from nothing,
so they carry no merge skew — but they do take the sweep lock, which is what the
number is sized on. When both queues want the last slot, the implementer takes
it: the coder idling is the more expensive of the two.

The lock has room for exactly this. `check_all.py` is serial on purpose
(`:40-45`) and takes ~97 s, so N subagents queue behind `/tmp/rev-sweep.lock` and
the last one waits up to N × 97 s: **at ten, about sixteen minutes**, against the
brief's wait loop of 180 iterations of 10 s, which gives up at **30 minutes**.
Ten fits with room; the arithmetic breaks somewhere past eighteen, which is the
real reason not to run fifteen.

What ten costs is not time, it is skew. **Every implementer branches from the
same `origin/main`**, so the more of them there are, the more are merging against
a base that has moved under them. Throughput is bought and paid for in conflicts
and in semantic merge skew.

**Ten is this stage's number and no other stage's.** It was sized by the sweep
lock and by merge skew, and the other two fan-outs have neither: triage analysis
takes no worktree and runs no sweep, and verification stops running one as soon
as you sweep each merged SHA once yourself. Both of those are bounded by #275
instead, and both say so where they are described. Carrying ten across to them —
which is what produced a pipeline of width one on 2026-09-12 — reads as caution
and is arithmetic applied to the wrong queue.

**So the constraint is files, not count.** Two implementers in one file is worse
than ten in ten, and going under ten is right only when what is left in the queue
overlaps what is already out. Check the target files before you fan out, and
check them against the repo rather than against the issue — on 2026-09-11 #181
went out against `tools/weather_check.py`, a file that has never existed.

### Deviating from the pick query

When you dispatch K implementing subagents in one tick, take the top K rows of
the pick query, in order and without gaps. That is normal operation and it posts
no comments.

A row inside the top K that you do not dispatch has been **passed over** —
whether you took a lower row instead or simply fanned out to fewer. Before the
tick's first subagent starts, post a comment on that row's own thread carrying:

- the pick query's output and the time you ran it, showing where the row sat;
- what you dispatched instead, by issue number;
- the specific technical reason. **File overlap with something already in flight
  is the reason this section has already blessed** — *the constraint is files,
  not count* — and so are an unmerged dependency sitting in another subagent's
  worktree, a blocker visible in the diff, and work that cannot start until the
  device is checked.

**Complexity aversion is not a reason.** Never skip a P1 to take a smaller or
easier P2. An issue at `stage:ready` has a thread carrying its patch and its
check cases; that is what the label means.

If an issue genuinely cannot be started because its analysis is incomplete or
wrong, it does not belong at `stage:ready`. Return it to `stage:triage` with the
gap named, then re-run the pick query. That is a different act from passing it
over, and it leaves its record in the same place.

The same applies to a stage change that hands the work to somebody else. Moving
an issue to `stage:owner` says a human has to decide before anything else can
happen, and a move with no comment leaves the next reader to reconstruct why.
One line naming what is blocked, and on whom.

> Measured 2026-09-12, and it is why this section exists. The pick query was run
> and it returned `P1 #239` first; a `P2` from the third row was implemented
> instead, and nothing on any thread recorded that the top of the queue had been
> passed over. The reviewer's own diagnosis, on #409: *low-hanging fruit bias* —
> #239 touched streaming, the WebSocket frames and `cards.js`, #212 was a
> localised defect in one file, and nothing charged for the difference. It cost
> twelve hours on the only open issue that changed what the terminal does for
> the household.

### Handed-back PRs at `stage:triage` (#654)

When the coder refuses a PR, it returns the issue to `stage:triage` with a
refusal comment and leaves the PR open (*"Handing back to reviewer at stage:triage.
Do not open a second PR — update this PR."*).

When triaging or dispatching from `stage:triage`:
1. **Check whether the issue already has an open PR**:
   ```bash
   gh pr list --search "$N" --json number,headRefName,mergeable,state
   ```
2. **Do NOT start from scratch or open a duplicate PR.** An issue with an open
   PR has already landed its branch; starting a fresh worktree from `main` discards
   the work and creates a colliding PR.
3. **Check out the existing PR branch**:
   ```bash
   git fetch origin <headRefName>
   git worktree add /tmp/rev-$N-wt origin/<headRefName>
   ```
4. Address the coder's refusal (e.g. fix the PR body's fail-before or sweep SHA,
   resolve behavioural conflicts, or add missing tests), run the sweep under lock,
   force-push the branch if code changed, update the PR body, and advance the issue
   back to `stage:pr`:
   ```bash
   gh issue edit <N> --add-label stage:pr --remove-label stage:ready,stage:triage,stage:owner
   ```

**Visibility interval for stalled handed-back PRs (criterion 4):**
If a handed-back PR remains parked at `stage:triage` with no reviewer pickup or update
for > **45 minutes**, it is stalled. The reviewer's tick checks for stalled handbacks
via the sweep above, ensuring parked PRs do not linger unnoticed while other issues are worked.

### When an implementer reports back

**Start its replacement before doing anything else with the report.** Not after
the two `gh` commands below, not after the verdict, not after the file-conflict
check — first, while the slot is empty. Pick from `stage:ready` by the rules in
[Implementing — the fan-out](#implementing--the-fan-out), dispatch, then come
back and finish the report. Dispatching costs one message; the report keeps.

This is the refill rule, and it lives here on purpose. It used to live only in
the fan-out section seventy lines up, which is read once per tick while planning
a batch — not when a subagent reports back. The event and its rule were in
different places, and the reviewer lapsed three times on one morning having read
the text. Moving it here is the fix; do not move it back, and do not settle for
making the copy upstairs louder.

**One exception, and it is the file rule rather than caution.** If every
remaining `stage:ready` issue touches a file an implementer already has out, the
slot stays empty and you say so on the tick. *The constraint is files, not
count* — but that means a specific overlap you can name, never a general sense
that the batch should settle first.

**It opened a PR.** Three commands, then it is the coder's:

```bash
# Without this line in the body the merge does not close the issue, and an open
# issue with no stage matches no query in the loop. Two of four PRs dropped it
# on 2026-09-12; both issues went silent with their work already in main.
gh pr view <PR> --json body -q '.body' | grep -q "Closes #<N>" || echo "PR <PR> will not close #<N> — add it"
gh issue edit <N> --add-label stage:pr --remove-label stage:ready
gh issue comment <N> --body-file /tmp/rev-<N>-pr.md
```

`gh pr edit <PR> --body-file` fixes a missing one. Do it before you hand the PR
over — after the squash there is nothing left to edit, because the branch commit
message is discarded and only the PR body ever carried the reference.

The comment says: the PR number, the base SHA it branched from, the sweep result
including **wiring's runtime in seconds**, the fail-before/pass-after counts, and
what the implementer could not settle. That last part is what the coder reads
before merging and it is the one section no later sweep replaces.

**Do not claim-check the PR body.** It is tempting — it makes claims, and this
skill claim-checks everything else — but the merge sweep re-runs all 22 scripts
at the merged SHA anyway, so a false green costs one verification round trip
instead of an audit on every PR. Spend the subagents on verdicts, which nothing
else re-runs.

**Read the *could not settle* section yourself, though.** An empty one on a
change that obviously needed the device is a reason to ask before the coder
merges, not after.

**It could not finish.** No PR. Comment on the thread with what blocked it, and
set the stage the truth earns: leave it at `stage:ready` if the patch is still
right and it hit an environment problem; move it back to `stage:triage` if the
analysis turned out to be wrong, and say which claim failed. An issue that keeps
coming back from implementers is an analysis defect, not an implementation one.

**Never leave it at `stage:ready` with a worktree still on disk.** The next tick
will dispatch it again and two implementers will fight over branch `issue-N`.

### Worktree and branch hygiene, every tick

Implementers die mid-run — a timeout, a compaction, a crash — and what they leave
behind is invisible until it collides with something.

```bash
git -C ${SHARED_REPO_PATH} worktree list
git -C ${SHARED_REPO_PATH} branch -r --list 'origin/issue-*'
L=/tmp/rev-sweep.lock
[ -d "$L" ] && echo "STALE mkdir-era lock, started $(cat "$L/started" 2>/dev/null)"
ps -Ao command | grep -c '[c]heck_all.py'   # 0 = nobody is sweeping
```

- **A worktree with no live implementer and no open PR** is a leak. Remove it:
  `git worktree remove --force /tmp/rev-<N>-wt`. This writes to the shared
  `.git`, and it is blessed for exactly the reason `worktree add` is — it is how
  worktrees work, and nothing else can do it.
- **A remote `issue-N` branch with no open PR** is a dead implementer's push.
  Delete it, or the next implementer for that issue fails on `worktree add -b`.
- **A zero-byte regular file at `/tmp/rev-sweep.lock` is not a leak — it is the
  lock.** The brief holds it with `fcntl.flock`, and an `flock` lives on the open
  inode, so the name persists after every release and says nothing about whether
  anyone holds it. **Never `rm` it.** Unlinking it under a live holder does not
  free anything; it makes the next opener create a fresh inode and sweep
  *alongside* the holder, which is two concurrent sweeps and two greens that mean
  nothing. `ps` for `check_all.py` is the only honest answer to "is anyone
  sweeping", which is why the probe above asks that instead.
- **A *directory* at that path** is a holder from the pre-2026-09-12 `mkdir`
  recipe — a session that has not reloaded the brief. The brief clears it once it
  is past 600 s. If one outlives that and `ps` is empty, say so on a tick: it
  means a session is still running the old recipe and the two schemes are
  colliding again.

`gh pr merge --delete-branch` cleans up the successful path, so everything you
find here is a failure that went unreported.

## The verify queue

**Detection query.** Closed `COMPLETED`, carrying the app label, not yet carrying
`stage:verified`:

```bash
LIM=500
gh issue list --label "<app-label>" --search "-label:stage:verified" --state closed --limit "$LIM" \
  --json number,title,labels,stateReason \
| jq -r --argjson lim "$LIM" \
    'if length >= ($lim*4/5) then "TRUNCATED: raise --limit" else empty end,
     (.[] | select(.stateReason == "COMPLETED")
          | select([.labels[].name] | index("stage:verified") | not)
          | "\(.number)\t\(.title)")'
```

**The query is unchanged by the redesign and that was deliberate.** The merge
closes the issue because the implementer put `Closes #N` in the commit message,
so *closed and not yet verified* still selects exactly the issues that need a
verdict. Inventing a `stage:merged` state would have thrown away three separately
measured defect fixes for nothing.

**The second query, since 2026-09-14: issues that merged and stayed open.**

```bash
gh issue list --label "<app-label>" --label "stage:live" --state open \
  --json number,title,updatedAt \
| jq -r '.[] | "\(.number)\t\(.updatedAt)\t\(.title)"'
```

These carry `needs:live`, their pull request said `Refs #N`, and the code is in
`main` — so nothing in the query above will ever return them. The gate is
described in the shared skill under
[*`needs:live`*](../issue-looping-shared/SKILL.md); what it means at this desk is
that **you cannot verify these yourself.** They need the Pi or a live model, and
this session has neither. Each one gets the same treatment as the stated hold
rule below: on the tick you first see it, post what the tester has to look at and
what would satisfy it, then leave it. When the tester's `live-verified:` line
lands, close it and add `stage:verified` in the usual way.

A row here that is older than a day with no `<!-- tester -->` comment on it is
worth saying out loud on the thread — that is the live queue silting up, and it
is the failure this whole mechanism trades against.

**Read the predicate carefully, because each clause is there for a measured
failure** (#211, from all 251 close events on the `ai-terminal` label,
reconstructed from the timeline API):

- **`stage:verified` by name, not "no stage at all", plus server-side filter.**
  `--search "-label:stage:verified"` filters out verified issues on the server so
  they do not consume slots in `--limit "$LIM"` (#264). The client-side filter
  remains as belt-and-suspenders against index lag. The old query asked for the
  *absence of any* `stage:` label, and **126 of 251 closes (50.2%) still carried
  `stage:doing`** — the label `stage:pr` replaced — **at the instant of close**,
  median 58 s, max 9 m 26 s before it was cleared. Those are not carelessness;
  see *the trailer rule* in the shared skill. The mechanism moved from `git push`
  to `gh pr merge` and the trap did not move with it. A second population is not
  transient at all: #198, #202 and #209 sat closed under `stage:triage`
  indefinitely, one of them closed by the monitor declining it on the merits,
  with all the time in the world to clear a label. Any predicate phrased as *no
  stage* is blind to both.
- **`stateReason == "COMPLETED"`.** The inverse error, and the one nobody had hit
  yet: fix the clause above on its own and the `NOT_PLANNED` closures get
  promoted to "closed, unverified", and you spend a verification subagent
  auditing a commit that does not exist. A declined issue is finished. Only a
  completed one has anything to verify.
- **`--limit "$LIM"`, and an 80% tripwire, because truncation is silent.** This is
  the clause that actually cost something. **`--limit 50` returns the newest 50** —
  no warning, no count, exit 0. **P0 #10 sat at position 161 and went unseen for
  a day and a half**, absent from every tick, because the query answered exactly
  what it was asked. Nothing reconciles an issue you have never been shown.

  A number chosen to clear the count is a number that stops clearing it. The
  first fix here was `--limit 200` against 170 closed issues; **six days later
  the count was 194 and this label was closing 64–71 a day**, so the margin was
  under one day and the same failure was about to recur with a different
  constant. Hence the tripwire: `length >= ($lim*4/5)` derives the threshold
  from the limit variable `$LIM` (giving an 80% warning tripwire before exhaustion)
  and makes the query *say so* instead of quietly shortening the queue (#264).

  **Without server-side filtering, 500 buys about five days, not months, and
  the countdown started at `4795ca6`** (#264). Measured on 2026-09-11: 197 closed
  against this label, so 303 of headroom, against 64 closed on the last full day —
  4.7 days, or 6.9 at the four-day average, and the rate is climbing rather than
  flat. Server-side `-label:stage:verified` keeps verified issues from filling the
  window, while the tripwire warns whenever unverified closed issues reach 80% of
  the limit. The number itself is not the safeguard and never was; **the tripwire
  is**, and it fires with warning margin rather than after truncation. When it
  does fire, raise the constant *and* re-read this paragraph, because a limit
  picked to clear today's count is the defect this whole section is about.
  Measured 2026-09-11: at `LIM=3` the query prints `TRUNCATED: raise --limit`
  above the results, and at `LIM=500` it prints nothing extra.

The three old defects compounded in one direction: an issue had to be recent
*and* unlabelled *and* not-yet-considered to be seen — while an old issue is
precisely the one most likely to have been closed under a different convention.

**One new population to watch, created by the redesign.** An issue that is closed
**and still carries `stage:pr`** means the coder merged and did not clear the
label. It is caught by this query either way, because the predicate names
`stage:verified` rather than asking for no stage. Clear the label when you post
the verdict, and if you see the same issue in that state on a second tick, that
is a coder defect worth a comment.

**Treat every read as possibly stale, and never act on an old one.** The other
sessions are writing to these issues while you poll, and the API does not always
show you the current state: on 2026-09-09 this session read #64 as open with no
comments when it had in fact been closed with four, then added `on hold` to an
issue that was already closed and verified — a label which is not there now. No
gh cache was configured, so this is replica or index lag on GitHub's side and it
can land on any tick.

Three rules follow, and they cost one extra call each:

- **Re-read immediately before you write.** A verdict formed three minutes ago
  was formed against a repo that has moved.
- **Read back after you write**, and confirm the change is actually there. A
  command that exits 0 proves the request was accepted, not that the state you
  wanted exists.
- **When a read decides a verdict or a label, use the timeline**
  (`gh api repos/OWNER/REPO/issues/N/timeline --paginate`) rather than the
  summary fields. It carries every label, close and reopen as a timestamped
  event, so it cannot quietly omit one, and it is how you reconstruct what the
  other sessions did.

The label is the state, so the query is stateless. It survives context
compaction, a session restart and a reordered backlog, and it needs no notes
file. An issue reopened and closed again correctly comes back for
re-verification.

### Iterating the detection query every tick

**The verify pass iterates the detection query, not the recent merge event.** A
reviewer session that only verifies what it has just watched close is
vulnerable to leaving issues indefinitely unverified (#522). On every tick,
run the detection query above:

1. For each row returned, inspect the issue and its timeline.
2. If the issue is ready for verdict (e.g. sweep passed, evidence satisfied),
   dispatch the verification subagent, claim-check, post the verdict and add
   `stage:verified` (or reopen).
3. **The stated hold rule**: If an issue returned by the query cannot be verified
   in that tick (e.g. held under `74cfbd2` second-pass gate awaiting a regression
   test case in `tools/regressions.yml`, or awaiting a tester screenshot under
   *A visual symptom is verified by the tester's run*), **you must post a comment
   on its thread stating what is holding it and what condition will release it**.
   A hold nobody can read is indistinguishable from a backlog (#495, #522).
   An issue may not be left in the verify queue silently.

## Running the merge sweep

*Who runs which suite* in the shared skill says why this one is yours: the branch
sweep is the implementer's and is already green by the time you see the PR, so
what you are measuring is **what merging produced**, which no branch sweep saw.
Extract the merged SHA and run it in `/tmp`:

```bash
D=/tmp/rev-<N>-tree; mkdir -p $D
git archive <merged-sha> -- <project-dir> | tar -x -C $D --strip-components=1
cd $D

# env_check.py needs a real .env, and it is in this sweep. Symlink, never copy.
ln -s ${SHARED_REPO_PATH}/.env $D/.env

PY=<repo>/<project-dir>/.venv/bin/python
export PYTHONPATH=$D/src                     # the venv's .pth points at the working tree
export PATH=/opt/homebrew/bin:$PATH          # js_check needs node
export PLAYWRIGHT_BROWSERS_PATH=$HOME/Library/Caches/ms-playwright

# Provenance. Must print a path under $D. Anything else and you are measuring
# the shared tree, not the commit. Import the package, not a module — importing
# `live.main` works too but logs its missing-credential warnings over the answer.
$PY -c "import ai_terminal, os; print(os.path.realpath(ai_terminal.__file__))"

$PY tools/check_all.py       # all 22, holding /tmp/rev-sweep.lock
```

Take the lock the same way the implementers do — the recipe is in the implementer
brief, and the reason it applies to you too is that an implementer sweeping in
parallel with you is exactly the contention `check_all.py:40-45` warns about. One
shell invocation, so the `trap` fires.

No worktree, no branch, no checkout, nothing touched in the repo — which is what
makes this compatible with the session being read-only. Verified as written
(minus the `.env` line and at 12 scripts) on 2026-09-09 at `d92dca8`: 54 s,
**1,276 cases, all green, nothing skipped**, provenance under `$D`, working tree
untouched throughout.

**All 22, and `env_check.py` is the exception you have to write down.**
`subagent-brief.md` forbids it, and the brief wins unless you override it *in
writing* in the specific brief you paste. So when you send a verification
subagent, say explicitly: *run `tools/check_all.py`, all 22 including `env`,
against the symlinked `.env`, holding the lock.* A subagent that silently drops
`env` reports 21 of 22 as a sweep, which is the exact shape of the defect the
owner's #288 ruling ended.

**Three ways this silently measures the wrong thing.** All three exit 0 and print
a plausible number, which is why the provenance line above is not optional.

- **The venv imports the working tree.** `uv run python` here resolves a venv
  whose `.pth` is the absolute path `…/src` — so
  `import ai_terminal` reaches the shared tree and the extracted SHA contributes
  nothing but the check scripts. Forcing `PYTHONPATH` fixes it; printing the
  resolved `__file__` is how you know it did.
- **A missing browser skips 12 cases without saying so.** `browse_check.py:1338`
  and `:1397` catch any launch failure and print `skipped: no Chromium …`, then
  carry on green. Measured on 2026-09-09: without `PLAYWRIGHT_BROWSERS_PATH` this
  reported `browse 187` against a true 199, the reviewer published the wrong
  number, and a round trip was spent explaining a 12-case delta between two SHAs
  at which the file is byte identical. The skill already says not to trust
  someone else's counts; this is the case for not trusting your own.
- **A tool missing from `PATH` does the same.** A subshell started for a sweep
  does not inherit an interactive shell's `PATH`, and `js_check` needs `node`.

So: read the provenance line, and compare the per-script counts against the
previous sweep before you publish. A count that *dropped* in an area the merge
did not touch is your environment, not the merge.

**Compare against the PR's own claim, not against a parent.** Fail-before /
pass-after now happens on the branch, where the unfixed tree still exists, and
the implementer reports both counts in the PR body. Your job here is different:
the merged total should be the pre-merge total plus what the PR added. A total
that is *lower*, or a script that is red, is what merging did — and with a batch
merge, the coder's merge comment names every issue that landed together, which is
your candidate set.

**Report `wiring`'s runtime as a number.** `check_all.py:47-51` puts it among the
18 scripts sharing 12 s, so tens of seconds means a real `generate_content` got
through — #287's fix does not fail red, so the timing is the only tell.

## The verification subagent

One subagent per issue. Start with the pasted read-only brief, then write the
half that is specific to this issue:

**How many at once: every closed-and-unverified issue the query returns, in one
message.** Owner's ruling, 2026-09-12, the same one that opened the triage
fan-out. The implementing cap of ten is not this queue's number. A verification
subagent takes no worktree, opens no branch and creates no merge skew — the two
things ten was actually sized against — so what is left to bound it is #275, the
secondary rate limit, and one comment plus one label edit each is a much lighter
burst than analysis.

**With one exception, and it is the reason this needed saying rather than just
doing.** The brief above tells each verification subagent to run the merge
sweep, and `check_all.py` is serial at ~97 s behind `/tmp/rev-sweep.lock`. Fan
out eight verdicts naively and you have not bought parallelism, you have bought
a queue: the eighth waits about thirteen minutes to produce numbers the first
one already had.

**So run the sweep once per merged SHA and hand the output to every subagent for
that SHA.** The 22 counts are a property of the commit, not of the issue, and
the coder merges in batches — *What else landed in the same batch* is already in
the brief because several issues routinely share one SHA. Sweep it yourself the
way *Reading the repo* describes, put the captured output and the SHA into each
brief, and say plainly: **the sweep result is given, do not run it.** That is the
whole of what the lock was serialising, so with it gone the width is #275's and
nothing else's.

Where a tick's verifications span several merged SHAs, that is one sweep per
distinct SHA — still a handful, never one per issue. If you find yourself about
to dispatch N subagents that would each sweep the same commit, you have found
the bug this paragraph exists to stop.

- **The issue number and how to read it** — `gh issue view N --json title,body,comments`.
- **The PR and the merged SHA to audit at**, plus `git show <sha>:./path` and a
  warning that the shared working tree is a different thing. `gh pr view <PR>
  --json files,body` gives the file list and the implementer's own claims in one
  call.
- **The bar**: the issue's "Done when" / acceptance sentence, and every concrete
  requirement in the body. Requirements buried in sub-bullets count.
- **The precedent**: an issue can be closed with no commit ever touching the
  relevant files. Verify with `git log` on those files. Never take a closing
  comment or a PR body's word for anything — check its factual claims against
  code.
- **The merge sweep**, spelled out as above: all 22, the `.env` symlink, the
  lock, the provenance line, and `wiring`'s seconds. Reconcile the counts against
  what the PR body claimed. A script red at the merged SHA is a behavioural gap
  whatever the branch said; a script whose count did not move is a case that was
  promised and not written.
- **What else landed in the same batch**, from the coder's merge comment. If the
  sweep is red, that list is where the cause is.
- **A hunting brief**: unhandled exception paths, races, state set but never
  cleared, frames arriving after their card is gone, orphaned tasks, off-by-one,
  anything crossing sessions. Point it at the specific new or heavily rewritten
  files.
- **The declined/dropped distinction**: something declined with a written reason
  is fine; the same thing dropped in silence is a gap. Make it say which.
- **The Architectural Intent Guard (#629)**: Read the entire issue thread for owner
  directives or architectural questions (e.g. *"why not use X directly?"*). Confirm
  the implementation honored the architectural directive rather than substituting a
  symptom patch (e.g. narrow regex or heuristic tweak).
- **Reconcile sweep validity (#629, #649)**: Validate that the PR body's sweep counts
  match real suites and baseline scale (~5350+ cases across 22 scripts). Never accept
  hallucinated suite names (e.g. `recap`) or fractional counts indicating broken environments
  or fabricated sweeps.
- **What "verified" means here** if the behaviour has no check script: read the
  code, and judge whether the PR body's manual verification was adequate.

**Do not tell it who wrote the code.** It does not need to know that an
implementing subagent of yours produced the diff, and telling it invites either
deference or overcompensation. It has the merged tree and the issue; that is the
whole of what a verdict rests on.

The review bar and the cause-and-fix requirement are in the pasted brief because
they never vary, and both are load-bearing. A subagent left to its own judgement
defaults to maximal scrutiny and hands you a PARTIAL over a docstring. And a
subagent that stops at the finding wastes the cheapest work in the loop: it has
the whole file in front of it and you do not, so its last ten minutes on the fix
are what turn a reopen into something an implementer starts on immediately
instead of re-deriving the diagnosis.

Ask for exactly this shape back:

```
VERDICT: VERIFIED | PARTIAL | NOT VERIFIED
Commit: <merged sha + one line on what it actually changed>
Sweep:  22/22, <total>, wiring <n>s | <script> red at <sha>
        <per-script counts, and any that did not move>

Holds
- <requirement> — <evidence at file:line>

Behavioural gaps
- <missing, weaker than asked, or dropped silently>
  Failure: <inputs, state, wrong output>
  Cause:   <file:line>
  Fix:     <the change, concrete enough to type>

Bugs spotted in passing
- <defect> — file:line, the concrete failure scenario, and the fix

Also (coverage and cosmetics, non-blocking)
- <unpinned line, stale comment, prose that drifted>

Not checkable from here
- <...>
```

**Check the load-bearing claims before you publish — with a second subagent, not
by hand.** Subagents get pathspecs wrong, cite line numbers from the working tree
instead of the audited commit, and occasionally assert something they inferred.
You are signing the comment, so the check is not optional. But it is `git show`
and `grep` over files you do not have in context, which is a subagent's job and
not the dispatcher's — see *Dispatching* in the shared skill.

Launch one **claim-check** subagent per report, cheap and narrow. Paste
[`../issue-looping-shared/references/subagent-brief.md`](../issue-looping-shared/references/subagent-brief.md),
then:

- the verdict's load-bearing claims, quoted, one per line — the ones the verdict
  turns on and the ones about to be asserted publicly;
- the SHA they must be checked at, and the reminder that the working tree is a
  different thing;
- the shape back: `CONFIRMED | REFUTED | NOT FOUND` per claim, each with the
  `file:line` it was confirmed at, and for a refutation, what is actually there.

Then you read two reports and act. If a load-bearing claim comes back REFUTED,
the verdict does not get published — send the original agent the refutation and
let it redo the affected part.

A claim-check is also the right tool for the small verifications that pile up
around a filing: re-printing every cited line range, confirming a script exists,
checking for a duplicate issue. Batch them into one agent rather than running
them yourself one `sed` at a time.

**Assert the mandatory `Collation:` token.** When claim-checking an issue draft
or a triage analysis, assert that the collation query was run and that the
`Collation:` token is present at the top (`Matches: #N...` or `none found`). If
the collation token is missing or blank, the claim-check returns REFUTED and the
draft cannot be published (#199).

## The review bar

Only a **behavioural defect** justifies a PARTIAL and a reopen: wrong output, a
regression, a case the fix misses, data loss, or a claim in the PR body someone
would act on and be misled by. If none survives, the verdict is VERIFIED.

Everything else goes in an `Also` section marked non-blocking. That includes:

- missing test coverage on code that is correct today — **except on a second
  pass over an issue that regressed; see the gate under *Acting on a verdict***
- comment and docstring wording, including a docstring that claims more than the
  code delivers
- `file:line` drift in a comment's prose, and off-by-one prose counts ("five
  cases" where there are six)
- style, naming and structure you would have done differently

A mutation no check script catches is a coverage hole, not a bug. Report it —
knowing which line nothing is watching is worth having — but do not reopen for
it. The code either does the wrong thing today or it does not.

Two reasons this matters more than it looks. A reviewer that reopens on nits
teaches everyone downstream to skim verification comments, and the real defect in
the next one goes unread. And every reopen now costs a full analyse-implement-
merge round trip; spend those on something that changes what the program does.

Unsure which side of the line something falls on? Write the concrete failure:
inputs, state, wrong output. If you cannot write one, it is not behavioural.

This applies to your own overrides too. A subagent that returns PARTIAL on a
coverage hole is reporting something real and getting the verdict wrong —
publish VERIFIED and keep its finding in `Also`.

**And it applies in the other direction now that you caused the work.** The bar
is the same for a diff your implementer wrote as for one it did not. Do not
soften a behavioural gap because fixing it means dispatching another implementer,
and do not manufacture one to look independent.

### The Architectural Intent Guard

When an issue or thread carries explicit owner directives or architectural questions
(e.g. *"Why do we re-derive a synthesized 'short name' through lossy reverse geocoding
heuristics instead of honoring the recognized/entered place name that the user set in the
settings panel?"* in #373):

- **The verification MUST confirm that the architectural directive was honored.**
- If the implementation substituted a narrow regex, a heuristic tweak, or a symptom
  patch while bypassing the owner's architectural instruction, the verdict **MUST NOT**
  be `VERIFIED`. Bypassing an owner directive in favor of a symptom patch is a behavioural gap.

### A visual symptom is verified by the tester's run, not by a suite

When the issue's acceptance sentence is about what a person sees — something
offscreen, cut off, unreadable, the wrong size, in the wrong place, not there —
**the evidence for VERIFIED is the tester's screenshot of the merged commit.** A
check script may accompany the verdict. It may not be the evidence for it.

The reason is not that the suites are untrustworthy in general. It is that this
class of assertion runs against a fake with no layout: `js_check.py`'s DOM fake
has no viewport, no `scrollTop` and no box model, so a check written for a
scrolling bug can only record the arguments the code passed.

```python
check("subsequent section on connected card scrolls into view with smooth/nearest",
      scroll_test["results"][1]["scrollCalls"], [{"behavior": "smooth", "block": "nearest"}])
```

That is green for every value, which is bad, and green for the *broken* value
specifically, which is worse: correcting `block` to `"start"` turns the suite
red, so the fix has to argue with its own check before it can land. **A check
that pins the defect as its expected value is itself a behavioural gap**, and it
belongs in the verdict as one rather than in `Also`.

So: no screenshot of the merged commit, no VERIFIED on a visual issue. Put it
under `Not checkable from here` and leave the issue closed-and-unverified for a
tick. The tester runs on its own cadence and one arrives; certifying a fake's
green does not become right for having been quicker.

> **Why it is here.** #501 was closed `stage:verified` at 00:22 on 2026-09-13,
> three minutes after the tester posted a screenshot of the same commit showing
> the video player still entirely below the fold — and the verdict cited the
> check above as its coverage. The tester reopened it 33 minutes later with a
> second screenshot. The pinned-value shape was #481 the day before, also in
> `js_check.py`. Filed as #507.

## Acting on a verdict

### Before you publish: read the thread

**A verdict may not be posted while an unrebutted report from the tester, newer
than the commit under test, sits on the issue.** Either the verdict names that
report and says why it does not hold, or the issue stays open and you dispatch
against it.

This costs one read of comments you already fetch — `gh issue view N --json
title,body,comments` is in the verification brief — and one look at the
timestamps before the label goes on.

The tester is the only role here that runs the product. Its reports are the sole
evidence that a person at the panel now sees the right thing, which is a
different question from whether the diff does what the issue asked, and no suite
answers it. A verdict that does not know the tester exists is answering the
second question and labelling it the first.

> **Why it is here.** Until 2026-09-13 this file did not contain the word
> "tester". The shared skill says what the role is for; the procedure executed
> every tick never consulted it. #501 was verified three minutes after the
> tester's screenshot of the same commit disproved it, with the screenshot
> unmentioned on a thread the verdict was posted to. Filed as #507; the owner
> chose to write the gate rather than treat it as a one-off.

### A second pass over a regressed issue needs a regression case

**An issue that once carried `stage:verified`, regressed, and is now closed
again may not return to `stage:verified` until a case exists that is red at the
commit the regression shipped on and green at the one you are verifying.** No
such case, no label: post the verdict, say what is missing by name, and leave
the issue closed-and-unverified until it arrives.

**This is a deliberate exception to the review bar.** Missing coverage on code
that is correct today is non-blocking, and stays non-blocking, because the
suite cannot chase every line that merely *could* break. A regression is not
that. The app has already demonstrated it can break this exact thing, twice, in
front of the owner — so the coverage is not speculative, and *"it is fixed
now"* is what the first `stage:verified` said.

The corpus this lands in is **`tools/regressions.yml`**, which the tester owns
and commits; the runner that reads it is #509. The division of labour:

- **The tester names the case.** It is the role that saw the symptom, so it
  writes what to drive and what to assert, in the issue's own words.
- **An implementer writes the code** if the entry needs one — a new selector, a
  new probe. The tester does not write Python into the repo.
- **You check it went red.** An entry nobody watched fail at the bad commit is a
  guess, and a guess in a corpus is worse than a gap, because the count goes up.

**Check the pinned values before you accept the entry.** A regression that
survived a `stage:verified` usually did so because a check asserted the broken
value, which means the entry has to be accompanied by a correction somewhere
else in the suite. #472's fix has to flip four `js_check.py` cases; #481's had
to delete two pins; #501's cited a `scrollIntoView` case that is green for every
value. **A check that pins the defect as its expected value is a behavioural
gap** — that rule is in the review bar and this is where it bites hardest,
because the pin is precisely what let the regression pass the first time.

> **Why it is here.** #472 closed `stage:verified` on 2026-09-12: starter chips
> flashed on wake, and the fix hid them at the top of `handleWake()` with
> nothing on the wake path to bring them back. On 2026-09-13 the owner found the
> panel with no chips at all — and the copy shipped the same day by #485 told
> them to *"tap a chip"*. Scenario 20 ran that path and reported `ok`, because
> `check_visibility_timeline` only faults an element that appeared and vanished;
> one hidden throughout is clean by construction. Four `js_check.py` cases
> asserted `suggested: False`, and no case anywhere asserted chips are visible
> after a wake. Nothing in the loop was wrong on its own. The owner's ruling was
> to stop re-verifying on the strength of a suite that already proved it cannot
> see this symptom.

**VERIFIED** → post the verification comment, add `stage:verified`, clear
`stage:pr` if the coder left it on, leave the issue closed. A verdict can carry a
long `Also` list and still be VERIFIED; only a behavioural gap withholds the
label.

**PARTIAL / NOT VERIFIED** → post the verification comment, then reopen with a
second comment listing what is left, and set the stage the reopen actually
earns:

- **`stage:ready`** when the second comment names the file, the line and the
  change — a reopen written to this skill's standard usually does. It goes
  straight back into the implementing fan-out on a later tick.
- **`stage:triage`** when it does not, because an issue nobody can start from is
  not ready however urgent it is, and marking it ready puts a dead end at the
  head of a queue you are now the consumer of as well as the producer.

Confirm with the owner the first time; once they say to reopen automatically,
stop asking.

**Defects unrelated to the issue's own scope** → a new issue with the app label,
a priority, a type and a stage. Do not bury a separate bug inside a verification
comment where it will be closed along with its host. Several small ones with no
common cause can share one cleanup issue.

## Feeding the queue

**You are what makes an issue startable, and now you are also what consumes it.**
`stage:triage` becomes `stage:ready` only when you analyse it, and `stage:ready`
becomes a PR only when you dispatch an implementer. Both halves are your debt.
**Since 2026-09-12 the triage half has an alarm on it** —
`.claude/hooks/triage_gate.py`, described at the end of this section. The
implementing half still has none: nothing goes red when you skip it, and the
verify queue just keeps looking busy.

**Four triggers, and an unblocked `P0` outranks all of them.** Run this before
each tick's other work, and **log what it returned in your tick report** —
`triage P0/5, ready P1/28`, one line. A verification is a closed issue and it
keeps; a P1 nobody can see cannot be un-missed.

The log line is not bookkeeping. Measured 2026-09-11: #302 (`P0`,
`stage:triage`, unblocked) sat **2 h 45 m with no reviewer comment** while
#334 — filed at 08:58, `P1` — was picked up **22 minutes** after filing. The
gate was running the whole time and nothing inside the loop could see the
inversion; it took an outside observer counting comments. A gate that reports
its own two numbers every tick makes this class of failure self-announcing.

```bash
# both sides in one shape: how many unblocked, and at what priority
for stage in ready triage; do
  printf '%s\t' "$stage"
  gh api "repos/OWNER/REPO/issues?state=open&labels=<app-label>,stage:$stage&per_page=100" \
    -q '[.[] | select(.pull_request|not)
             | select(.issue_dependencies_summary.blocked_by==0)
             | [.labels[].name] | map(select(test("^P[0-9]$"))) | .[0] // "P9"]
        | [(sort|first), ([.[]|select(.=="P0" or .=="P1")]|length)] | @tsv'
done
```

1. **An unblocked `P0` sits at `stage:triage`.** It is dispatched on this tick,
   ahead of everything else, and it does not wait for a tie, a count or a third
   sighting. This trigger exists because the other three are all comparisons or
   thresholds, and a single P0 satisfies none of them.
2. **Triage ranks at or above the startable queue.** Not *above* — **at or
   above**. A tie fires the gate.
3. **More than three unblocked P0/P1 sit at `stage:triage`.** Then triage gets
   analysis subagents every tick until it is back under three, whatever
   `stage:ready` holds and however deep the verify queue is.
4. **An unblocked P0/P1 has been at `stage:triage` across three of your ticks.**
   It goes out first and ahead of the verify queue.

**Firing the gate is not taking the tick** (see [The tick](#the-tick)). Triage
analysis and implementers go out together, up to the cap; what triggers 2-4
buy is a place in the fan-out, not exclusive use of it. The one thing that
outranks trigger 4 is an empty `stage:pr` with a non-empty `stage:ready`,
because that one is measured in a Pi that gets nothing.

**Trigger 1 is the exception to that paragraph**, which is the point of having
it: an unblocked P0 does not queue for a slot behind anything, including an
empty `stage:pr`. A P0 is the label for work whose absence is being felt right
now, and every other rule here is about fairness between queues.

**Why a tie has to count, which is the clause this gate was missing.** The old
rule fired only on a strict inversion, and a strict inversion is the one state
this queue does not reach. Both sides are fed by you, at the same bar, out of the
same verifications — so they converge to the same priority and sit there.
Measured 2026-09-11: `stage:ready` held **5 unblocked P1s and 28 P2s** while
`stage:triage` held **5 unblocked P1s** (#186, #213, #243, #248, #250), and
`sort|first` returned `P1` on both sides. No inversion, so verification took the
tick — correctly, by the old wording, and with five P1s invisible. Earlier the
same day it read P0-ready against P1-triage, which is also not an inversion. **A
comparison of maxima cannot see depth**: one P1 in triage and twenty read
identically, which is why trigger 2 counts instead of comparing.

> Measured on 2026-09-10, and the reason this section exists at all. Six P1s —
> #138 through #143 — were filed at `stage:triage` at 06:29 and were still
> untraced ten hours later, while the reviewer posted 21 verification comments
> across nine closes. It was never idle, so the wording before that (*"when you
> run out of work"*) never fired once, and the tick's step 2 said `stop` before
> it could. By 08:20 the queue read: one P1 startable and already `stage:doing`,
> six P1s invisible, six P2s ready. The next pick after #10 was a P2. **A gate
> that only opens when you are idle never opens** — and the 09-11 measurement is
> the same lesson one turn further in: a gate that only opens on a state the
> system does not reach never opens either.

**An issue is drained when the label moves, not when a comment lands.** Trigger 2
counts `stage:triage`, so an analysis comment that stops short of `stage:ready`
buys nothing — the issue is still invisible to the implementing query, which is
the only thing that mattered. #248 carries three comments, one of them your own
*"patch is next"*, and has been startable-in-principle and unstartable-in-fact
since 21:19. Partial analysis is the same debt with more text on it. Either
finish it onto `stage:ready` or say on the thread what it is waiting for and
dependency-block it, so it leaves the unblocked count honestly.

**Issues you file yourself are the same debt, and they grow as you succeed.**
Verification is what finds them, so the better your verification tick, the longer
this queue gets — the mechanism is a feedback loop, not bad discipline. The
bound: **do not end a tick having filed more unblocked P0/P1 issues at
`stage:triage` than you moved off it in that tick.** File three and drain none
and the next tick is a triage tick, no exceptions. Filing at `P2` is not a way
around this — it is the honest answer when the finding is real and not urgent,
and the shared skill's *when torn between two, pick the lower* already says so.

The work itself: one analysis subagent per **`stage:triage`** issue, each
producing the investigation an implementer would otherwise do first.

**How many at once: every unblocked issue the query returns, in one message.**
The owner's ruling of 2026-09-12. Not three, not what is left over after the
implementers have taken their slots — one each, dispatched together.

**The implementing cap of ten does not bind here**, and the arithmetic that sized
it says why. Ten came from the serial merge sweep queueing behind
`/tmp/rev-sweep.lock` and from the merge skew of many worktrees branched off one
`origin/main` — see [Implementing — the fan-out](#implementing--the-fan-out). An
analysis subagent takes no worktree, runs no sweep and creates no skew; it is
read-only until it writes its verdict. Reusing ten here would be borrowing a
number from a constraint that is not present.

**What does bind is #275.** Each analysis ends in a comment plus two label edits,
so a wide fan-out lands a burst of content-creation writes, and GitHub's
*secondary* rate limit answers — the one `gh api rate_limit` does not report, so
the advice to check it and sleep until reset costs an hour for nothing. Tell each
subagent to write its comment and its label change and stop. This is the one
cost the implementing fan-out never exposed, because an implementer writes GitHub
exactly once, at `gh pr create`, and spends the rest of its life in a worktree.

**This one is enforced, since 2026-09-12.** `.claude/hooks/triage_gate.py` is a
`Stop` hook: at the end of a reviewer tick it runs the two-number query below and
refuses to let the turn end while more than three unblocked P0/P1 sit at
`stage:triage`, naming them in the refusal. Threshold and direction are trigger
3's, so the hook and the prose cannot drift into disagreeing.

It exists because the prose did not work. Measured 2026-09-12: six issues were
filed between 07:05Z and 07:30Z, five of them unblocked P1, and at 07:30Z every
one still had zero reviewer comments while `stage:ready` held two P2 leftovers.
The day's whole output was one analysis and one PR. Three of the four triggers
were firing throughout. That is the fourth recurrence of the same failure on one
morning, and the first three were each answered with another paragraph — which
is the move the shared skill's own conclusion warns against.

The hook fails open on everything: no role, no `gh`, a timeout, a bad payload. It
also stands down after three consecutive refusals for one session, so a queue
that genuinely cannot be drained does not wedge you; it becomes the monitor's
problem, which is the role whose unit of work is the backlog.

```bash
gh api "repos/OWNER/REPO/issues?state=open&labels=<app-label>,stage:triage&per_page=100" \
  -q '.[] | select(.pull_request|not)
          | select(.issue_dependencies_summary.blocked_by==0)
          | select([.labels[].name]|index("on hold")|not)
          | select([.labels[].name]|index("hold:monitor")|not)
          | "\([.labels[].name]|map(select(test("^P[0-9]$")))|.[0] // "P9")\t\(.created_at)\t#\(.number)\t\(.title)"' \
  | sort
```

**The `sort` is load-bearing and its absence cost two and a half hours.** The
Issues API returns `created_at desc` by default, so the unsorted form of this
query hands you the *newest* triage issue first and the oldest last — the exact
inverse of the order you want. Measured 2026-09-11: #302, a `P0` and the oldest
unblocked issue in the queue, sat at the bottom of every tick's output for
2 h 45 m while three newer P2s were dispatched from the top. Nothing was skipped
and no trigger was wrong; the rows arrived in the wrong order and the top of the
list was taken. **Sort on priority first, then `created_at` ascending, and the
row you want is row one.**

Two details worth keeping as written. `test("^P[0-9]$")` and not
`startswith("P")`, because the latter would read a future `Pending` or `Perf`
label as a priority — the shared skill specifies this axis by shape for the same
reason. And `// "P9"` parks an unlabelled issue at the bottom rather than
dropping it, which is what you want: an issue with no priority is still an issue.

That query is also the answer to *have I already done this one*. `stage:ready`
and `stage:pr` are both off it, so a restarted session cannot re-analyse an issue
it has already analysed, or spend a subagent on one an implementer is writing
right now.

**The two `select`s are the same guards the pick queries carry, and they are
load-bearing here for a different reason.** `stage:triage` holds two populations
that no label tells apart: an issue nobody has looked at, and one the reviewer
looked at and deliberately parked behind a blocker — which is exactly what the
`type:spike` gate in
[`../issue-looping-shared/references/labels.md`](../issue-looping-shared/references/labels.md)
tells you to file. Without the `blocked_by` filter the next idle tick picks the
parked one up and writes the body the gate exists to defer, and the session that
does it is the one that parked it. `blocked_by` counts open blockers only, so the
issue re-enters this query by itself the moment its blocker closes; a label would
have to be remembered and removed by hand. `on hold` is off the query for the
reason in the ownership table: the reviewer does not chase it.

**`hold:monitor` is off it for a different reason, and it is one you may argue
with.** That label (2026-09-13, #490) is the monitor saying *this patch should
not land*, and the guard refuses the merge while it is on. It is aimed at
patches, so it will most often land on an issue *you* triaged and whose
implementer is already running. Three things follow:

- **Read the newest `<!-- monitor -->` comment before doing anything else.** It
  is required to say what is being contested and what would lift it.
- **Declining is a legitimate answer.** Post the counter-argument on the thread
  and say you are not changing the patch. The monitor is obliged to answer
  within a tick and to lift a hold it cannot defend; a disagreement the two of
  you cannot settle goes to the owner, which is what the monitor's ping is for.
- **Do not route around it.** Not by re-triaging the issue, not by splitting it,
  not by having an implementer open the PR anyway. It buys one round trip, and
  paying it is cheaper than the alternative measured on #476: the contested
  function shipped to the device, and both halves had to be untangled afterwards
  in #489.

Same read-only brief at the top of each — **analysis is not implementation, so
this is `subagent-brief.md`, not the implementer's.** The question is not "did
this land?" but:

- **Collation against existing records**: query open issues for the primary
  symbol (`_told`, `maps_search`, `_seen`, etc.). Record the mandatory
  `Collation:` token (`Matches: #N...` or `none found`) at the top of the
  comment. If the analysis changes an existing recommendation, name every open
  issue whose body or comments carry the earlier one (#199).
- **What is actually causing this**, traced through the code to a line.
- **What exactly to change** — the file, the line, the new code, handed back as a
  unified diff against a named SHA rather than described. Where a choice exists,
  try the candidates and recommend one, saying why the others lost.
- **The check cases that decide whether it worked**, run both ways under `/tmp`
  with both counts reported. Same rules as *Check cases* in the shared skill:
  `.py` where code can reach it, an `.md` procedure with expected observed output
  where it cannot.
- **What the issue got wrong.** Issue bodies are written from memory and from
  reading. Claims about a library's behaviour are the ones that waste the most
  time, so make the agent check them against the *installed* package source and
  report version and `file:line`. A wrong premise caught here is a day saved.
- **What can be measured offline right now** — timings, mutation runs, exhaustive
  sweeps, the candidate fix run against a reproduction. Work in `/tmp`, on
  copies. Every number the agent produces is a number the implementer does not
  have to go and get.
- **What could not be settled without the device, the network or credentials.**

Fan them all out in one message so they run in parallel.

Then claim-check them — one agent over all the reports, not one per report, and
not by hand, asserting that every comment carries the mandatory `Collation:`
token — post one comment per issue, and **move the issue to `stage:ready`
in the same breath as posting.** The label is the claim that the comment you just
wrote is implementable. The bar for a *recommendation* is lower than for a
verdict: check the structural claims the recommendation rests on rather than
every line number. You are still signing it, but you are proposing, not ruling.

If the analysis comes back without a patch — the agent could not settle it, or
the answer needs the device — leave the issue at `stage:triage`, post what it
found, and say what is missing. `stage:ready` is not a summary of effort spent.

**Audit every acceptance criterion for `needs:live` (#652):** During triage
analysis, audit each acceptance criterion against what `tools/*_check.py` can
execute offline. If any criterion requires the physical 10" touchscreen kiosk,
hardware, microphone, camera, real reboot/GPIO, or live model/OAuth network
rounds, attach `needs:live` immediately at triage (`gh issue edit <N> --add-label needs:live`).
Unit tests covering isolated logic do not exempt the issue if any live criterion
remains.

**Write the comment as the thing your implementer will work from**, because that
is exactly what happens to it: the implementing subagent applies what is on this
comment rather than re-deriving the diagnosis. So name the file and the line,
give the patch and its check cases, say which candidate you rejected and why, and
separate what you measured from what you inferred. When the recommendation
contradicts the issue body — a wrong premise, a function the body says exists
that does not — put that first and label it.

**Analysis is still not implementation, and the split is not bureaucracy.** The
analysis subagent builds its candidate fix under `/tmp`, on copies, and hands
back a diff; the implementing subagent applies it in a worktree, sweeps all 22
and opens the PR. Two reasons to keep them apart: an analysis that turns out to
be wrong costs nothing to discard when no branch exists, and the patch crossing
the boundary is what gets it *read* by a second context before it becomes a
commit. See *Patches cross the boundary* in the shared skill for the mechanical
traps in moving a diff between the two.

## The two comments you write

Signing, and the `file:line` rule that applies to both, are in *Signing your
comments* in the shared skill.

**Verification comment.** `## Verification`, then a bold verdict line naming the
merged SHA, then the commit and what it actually changed, then the merge sweep
with `wiring`'s seconds, then `Holds` / `Behavioural gaps` /
`Bugs spotted in passing` / `Also` / `Not checkable from here`.

Lead `Holds` with the requirement, not the code. Say what the commit did *well*
where it did — a reviewer that only ever reports faults gets discounted, and
noticing that a caveat was handled tells the next implementer the caveat was
read. Under `Behavioural gaps`, quote the issue's own words for what was asked;
the gap is the distance between that sentence and the code. Keep `Also` short and
clearly optional, so nobody has to guess which items block the label.

**Reopen comment.** Open by naming what landed and does not need redoing —
otherwise the next implementer re-reads the whole thing looking for what changed,
against a base that has moved since. Then numbered items, each with: the evidence
at `file:line`, a concrete fix (not "handle this better"), and why it matters in
one sentence. Close with how to verify. Point out when two items share a root
cause and should be fixed once.

For each item, **name the check case that would have caught it** — the existing
one that should have failed and did not, or the new one that has to be written. A
behavioural gap that survived a green 22-of-22 branch sweep is telling you
something about the suite as well as about the commit, and this is the cheapest
moment to say which.

## Filing an issue the owner asked for

**This is the common case and it is not the section below.** When the owner says
*"create a P2 issue: X"*, they are handing you a requirement, not reporting a
defect you then have to prove. File it and stop:

```bash
gh issue create --title "<the symptom, in the owner's terms>" \
  --body "$(cat <<'EOF'
<!-- reviewer -->
Owner's request: *"<their words, verbatim>"*

Filed unanalysed. Nobody has traced this to a line yet.
EOF
)" --label "<app>" --label "P2" --label "type:bug" --label "stage:triage"
```

The owner's words go in **verbatim**, because that is the requirement and it is
the one thing no later subagent can reconstruct. Everything else — the app label,
a priority, a type, `stage:triage` — is a label. Nothing else goes in.

**Do not trace it to a line, do not write a patch, do not write check cases, do
not verify the citations.** All of that is what
[Feeding the queue](#feeding-the-queue) already does, one subagent per
`stage:triage` issue, in parallel, off your context. Doing it inline instead
makes the owner wait twenty minutes for a filing that takes thirty seconds, and
it spends the dispatcher on work a subagent does better.

Two rules on top:

- **Owner-filed `P0` jumps the triage queue.** It gets an analysis subagent on
  the next tick whatever else is in flight.
- **Do not silently re-scope.** If the request rests on something you already
  know is wrong, file it as asked and say so in one sentence underneath. The
  owner decides; you are not the gate.

Filing a placeholder ahead of a spike gate uses this same shape.

## Filing an issue you found yourself

**Delegate the whole issue to a subagent.** Do not write the body out of what you
happen to have in context — you found a symptom, and a symptom is the start of
the work, not the deliverable. One subagent per defect, on the **read-only**
brief. Ask for everything an implementer would otherwise do before typing the
first character of the fix:

- **Read the current design and implementation** around the defect, not only the
  line it fires on. Which module owns this, what it already assumes, who else
  calls it, and which of those callers the fix would move under them.
- **Decide how to fix it.** Where more than one approach exists, name them, try
  them, and recommend one with the reason the others lost. A rejected candidate
  with a reason stops the next agent rediscovering it.
- **Build the fix and run it, under `/tmp`.** Copy the affected files out, write a
  reproduction that fails before the change and passes after, and say so in those
  terms. This is the part that earns the delegation: a recommendation that has
  been executed can be applied in minutes, and one that has not is a hypothesis
  somebody has to re-derive.
- **Write the check cases that pin it**, in the repo's own style, and run them
  both ways under `/tmp` — failing on the copied-out original, passing on the
  patched copy. Report both counts.
- **Hand back the patch and the check cases as text**, never as an edit. This
  agent is read-only in the repo, so what comes back is a diff in the issue body.
  Make it apply cleanly: unified diff, real paths from the repo root, generated
  against the SHA the audit was done at, and that SHA named next to it.
- **Check the premises.** Anything the fix rests on about a library's behaviour
  gets checked against the *installed* package source, with version and
  `file:line`.
- **Say what it could not settle** — the device, the network, a real account,
  anything needing the live server up.

Fan several out in one message when you have several defects. Then claim-check
the load-bearing claims the way you would a verdict — in a subagent, batched
across all of them, including every cited line range in the body — and file.

**Before you file, check the three gates.**
1. **Collation gate.** Query open issues for the primary symbol using the
   collation query. State the result with the mandatory `Collation:` token in
   the issue body (`Matches: #N...` or `none found`). If duplicates exist,
   forward findings to the existing issue instead of filing (#199).
2. **Spike gate.** Is any open `type:spike` going to decide this issue's design?
   Then it is not fileable yet — see `type:spike` in
   [`../issue-looping-shared/references/labels.md`](../issue-looping-shared/references/labels.md).
3. **Ordering gate.** Does it have to land after something else? Then record that as a dependency on
   the new issue as soon as it exists, not as a sentence in the body, or it will be
   picked up out of order.

What comes back goes in nearly as-is, held to the same standard as the comments:

- Mandatory `Collation:` token at the top of the body.
- Title says the symptom, not the fix.
- Body: where it comes from (traced through the code, with line numbers), why it
  matters, the fix with its caveats, how to verify.
- A concrete failure scenario — inputs and state in, wrong behaviour out. "This
  could fail" is not a bug report.
- **The patch**, in a fenced diff block, against a named SHA.
- **The check cases**, and the before/after counts from running them.
- **What was run under `/tmp` and what it showed**, kept separate from what was
  inferred.
- An explicit **Not in scope** section for anything considered and left out, with
  the reason. It stops the question being reasked and it stops the next agent
  gold-plating.
- Labels: app, priority, type, and `stage:ready` — an issue filed to this
  standard is ready by construction, since the patch and its counts are already
  in the body. File it at `stage:triage` only when you are opening a placeholder
  ahead of a subagent, which the spike gate above sometimes forces.

The target is an issue an implementing subagent can work from without
investigating: read the recommended fix, sanity-check it, apply the patch, watch
the check cases go from failing to passing, sweep, open the PR. If the body still
leaves it to work out *what* to change, or to invent the test that proves it
worked, the subagent stopped too early and the issue is not ready to file.

## Standing rules — reviewer only

The rules that bind all four roles are at the end of the shared skill.

- **You are read-only. Your implementing subagents are not.** They write only
  inside their own `/tmp` worktree, never the shared checkout, and they never
  merge, deploy or touch an issue.
- The brief decides which kind of subagent you get. `subagent-brief.md` reports;
  `implementer-brief.md` opens a PR. Paste, never retype.
- You verify work you caused. The bar does not move for it, in either direction.
  A PARTIAL on your own implementer's diff is the system working.
- `/tmp` paths are prefixed `rev-`; the other sessions write there too.
- You are a dispatcher. More than about three tool calls, or any call that opens
  a source file, is a subagent — chores included, and however fast you could do
  it yourself.
- The check on a subagent is another subagent. Delegating is not trusting, and
  re-deriving its work by hand is not checking, it is doing the work twice.
- Ten subagents in flight, as a target and not a ceiling, implementers first.
- Count them with `git worktree list`, never from memory, as the tick's first act.
- Sweep `--state open --label stage:verified` every tick. A hit is an issue no
  queue can see; clear the label, which only you can do, and triage it.
- A subagent reporting back is a dispatch, and the report comes second. Waiting
  for the batch to drain is the named failure.
  Start a replacement the moment one reports back. The bound is conflicts and
  merge skew, not the lock, so two in one file is worse than ten in ten.
- Clean up worktrees, dead `issue-*` branches and stale locks every tick. A dead
  implementer leaves no other trace.
- Re-arm `schedule(DurationSeconds=270, TimerCondition="any")` at the end of
  every tick; 1200–1800 s only when all three queues are empty. Never slow the
  poll instead of draining a queue. Stopping is not re-arming.
- No queue takes the tick. Every queue that fires gets its subagents on the same
  tick, because they run in parallel; the cap is what limits them, not priority.
- A coder with nothing to merge is the one thing that goes first. `stage:pr`
  empty and `stage:ready` not means implementers go out before anything optional.
- The triage gate fires on any of three triggers: it ranks **at or above** the
  startable queue, more than three unblocked P0/P1 sit there, or one has sat
  there across three of your ticks. Not idleness, and not a strict inversion — a
  gate that only opens on a state the system never reaches never opens.
- Never end a tick having filed more unblocked P0/P1 at `stage:triage` than you
  moved off it.
- `stage:ready` claims the analysis is done and an implementer can start. It is
  not a record of effort spent, and an issue is drained when the label moves, not
  when a comment lands.
- An owner's request is filed, not investigated: verbatim quote, four labels,
  `stage:triage`, one call.
- Do not file an issue whose design depends on a `type:spike` that is still open.
- Run the merge sweep at the merged SHA on a `git archive` copy under `/tmp`, all
  22 with a symlinked `.env`, holding the lock, and say so explicitly in the
  brief — the read-only brief forbids `env_check.py` and the brief wins unless
  you override it in writing.
- Force `PYTHONPATH` and read the provenance line before you believe a sweep
  count. The venv's `.pth` points at the shared working tree, and a sweep
  measuring the wrong tree exits 0. A skipped case is a green case.
- Report `wiring`'s runtime in seconds. That fix does not fail red.
- Only behavioural defects reopen an issue. No concrete failure, no gap.
- Read the thread before publishing a verdict. An unrebutted tester report newer
  than the commit under test blocks `stage:verified` until the verdict answers it.
- A visual symptom is verified by the tester's screenshot of the merged commit,
  never by a suite. A fake with no layout cannot see what the user sees, and a
  check that pins the broken value is a gap, not coverage.
- An issue that regressed after `stage:verified` does not get the label back
  until a case is red at the bad commit and green at yours. The tester names it,
  an implementer writes it, and it lives in `tools/regressions.yml`. This is the
  one place missing coverage is blocking.
- Declined with a reason is not the same as dropped in silence.
- The acceptance sentence is the bar. If the commit changed what "done" means,
  that is a gap even when the new behaviour is better.
- Every issue filed and triage analysis posted must carry the mandatory
  `Collation:` token, verified by claim-check. An empty check is recorded as
  `none found`, never omitted.
- Architectural directives from the owner outrank local symptom patches (#629, #373).
  Verify that the architectural intent was satisfied, not just that a narrow regex passed.
- Validate PR sweep counts against real suites and baseline totals (~5350+ cases across
  22 scripts). Never accept fabricated or partial sweep lines (#649).
