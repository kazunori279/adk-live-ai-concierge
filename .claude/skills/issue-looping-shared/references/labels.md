# Label values — what each axis means and how to pick one

The axes themselves, the ownership table and the two axes read on every tick
(`stage:` and `on hold`) are in [`../SKILL.md`](../SKILL.md). This file is what
you read at filing time.

## App label — derived from the working directory

One label per project so a shared repo can host several. The link between
directory and label is recorded in the label's own description, which makes the
lookup exact:

1. `gh label list --json name,description` and find the label whose description
   names the current directory's basename. Use it.
2. If none exists, derive one: strip a leading `YYYY-MMDD-` or `YYYY-MM-DD-`
   date prefix from the basename, then drop any remaining prefix that describes
   the hardware rather than the project. Create it with a description that ends
   in the directory name in parentheses.

Worked example: `.` → label `ai-terminal`, described
as `Raspberry Pi AI terminal (.)`. Step 2 is a guess;
step 1 is not, which is why the description carries the directory.

Every issue you open gets this label. Every query you run filters on it.

## Priority — P0, P1, P2

**P2 is the default. P0 and P1 are earned.** Owner's rule, 2026-09-11. It
replaces *"`P1` — default for anything that is not obviously P0 or P2"*, which
made the top of the queue the fallback and duly filled it: on the morning it was
replaced, nine issues were `P1` and four of them were the loop working on the
loop.

Two things earn a rank above `P2`, and nothing else does.

1. **High impact — it will affect many other issues and the product's outcome.**
   The test is leverage, not importance: how much other work is wrong, blocked or
   wasted for as long as it stands. An issue that degrades every issue the coder
   touches qualifies. A correct cleanup that changes nothing outside itself does
   not, however cheap it is to do.
2. **The owner opened it.** Those are `P1` on filing whatever else they are, and
   only the owner lowers them again.

| Label | Means | Test |
|---|---|---|
| `P0` | Broken now | Someone using the thing today hits it, or another issue cannot start until it is fixed |
| `P1` | Earned its rank | Clause 1 or clause 2 above |
| `P2` | Everything else | The default, and not a criticism: cleanup, polish, stale docs, correct work nobody is waiting on |

Silent failures are P0 even when rare: the user cannot report what they cannot
see. A cosmetic bug on the main screen is not P0 — annoying is not broken — and
it is only `P1` if clause 1 holds. When torn between two, pick the lower and say
why in the issue.

**How to tell whether the owner opened it.** The owner and all three sessions
write to GitHub as the same login, so `.user.login` cannot separate them. Two
signals, in this order:

- **The body's role marker.** A session opens an issue body with
  `<!-- monitor -->`, `<!-- reviewer -->` or `<!-- coder -->`, the same marker it
  signs comments with. A marker present means a session filed it.
- **A missing marker is not proof.** #285 and #286 were filed by the reviewer
  with no marker in the body. If the marker is absent and you did not watch the
  issue arrive, ask rather than promote.

An issue the owner **asked a session to file** is theirs under clause 2 — the
session was the typist. Nothing in the metadata records that, so the session
filing it says so in the body.

## Type

Exactly one, always prefixed:

| Label | For |
|---|---|
| `type:bug` | Behaviour that is wrong, including "works but lies" |
| `type:feature-request` | Something that does not exist yet |
| `type:documentation` | Prose that no longer matches the code |
| `type:spike` | Measure or investigate, with no committed outcome |
| `type:chore` | Refactors, dependency bumps, cleanup with no user-visible change |

That is the whole list. Check `gh label list` before creating anything, and
prefer widening one of these five to minting a sixth — **a synonym splits every
query written against the original**. GitHub's own `enhancement` is the one that
got in that way and had to be merged back into `type:feature-request`.

**`type:spike` means no committed outcome, and that constrains what may be filed.**
An open spike is a question, so any issue whose design depends on its answer has
no design yet. Do not file that issue while the spike is open. Filing it early
costs a rewrite of the part that matters: measured here on 2026-09-08, #63 was
filed ten minutes before the spike it depended on, and its body then needed two
separate *"do not start this from the body above"* comments inside ninety
minutes as #64's answer landed and moved. Under the patch rule this gets worse,
because what goes stale is a diff rather than a paragraph. Either wait for the
spike to close, or file a title and the blocker alone and let the subagent write
the body afterwards.

## Closing reason — GitHub's, not a label

Closing an issue that will not be worked is `gh issue close --reason "not planned"`.
It sets `state_reason`, which renders in the UI as a different close icon, is
returned on every REST read, and cannot drift from the close the way a label can.
Do not mint `wontfix`, `obsolete` or `duplicate`; all three re-implement it.

Say what overtook the issue in a comment before you close it. The field records
*that* it was not planned; only the thread records why.

## Blocking — a dependency, not a label

Ordering between issues goes in GitHub's **issue dependencies**, not in prose and
not in a label. A comment saying *"land #65 first"* is invisible to the pick
query and contradicts it outright: oldest-first within a priority would have
taken #62 before #65 and #63 before #64 on 2026-09-08. Both went the right way
only because a session happened to read the whole thread.

```bash
# #60 cannot start until #10 lands. The API takes the blocker's id, not its number.
ID=$(gh api repos/OWNER/REPO/issues/10 -q .id)
gh api -X POST repos/OWNER/REPO/issues/60/dependencies/blocked_by -F issue_id=$ID

gh api repos/OWNER/REPO/issues/60/dependencies/blocked_by -q '.[] | "#\(.number) \(.state)"'
gh api repos/OWNER/REPO/issues/10/dependencies/blocking    -q '.[] | "#\(.number)"'
```

Not **sub-issues**. Those express decomposition — *part of* — and every case here
is ordering instead. The deciding difference is which way each one
reports: `issue_dependencies_summary.blocked_by` sits on the blocked issue, so
the coder's pick query filters on it directly, while `sub_issues_summary` tells a
*parent* about its children and never tells a child whether it may start.

The reviewer adds the dependency when it files or when it discovers the ordering.
The coder does not remove one to unblock itself; if the ordering is wrong, say so
on the thread and let the reviewer correct it.
