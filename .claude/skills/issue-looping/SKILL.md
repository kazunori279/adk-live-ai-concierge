---
name: issue-looping
description: "Superseded pointer. The issue-looping skill is now four skills — issue-looping-shared plus one per role. Read issue-looping-shared/SKILL.md, which dispatches on CC_ROLE. Kept so that a session which read the old single file before 2026-09-10 and goes back to it after a compaction finds the new layout instead of nothing."
---

# Issue looping — split into four skills

This single file was split on 2026-09-10. Nothing was dropped; it moved.

**Read [`../issue-looping-shared/SKILL.md`](../issue-looping-shared/SKILL.md).**
It holds the hard limits, the label scheme, the dispatcher rule, who runs which
check suite, comment signing and check cases, and it dispatches on `CC_ROLE` to
the one role skill that is yours:

| `CC_ROLE` | Skill |
|---|---|
| `coder` | [`../issue-looping-coder/SKILL.md`](../issue-looping-coder/SKILL.md) |
| `reviewer` | [`../issue-looping-reviewer/SKILL.md`](../issue-looping-reviewer/SKILL.md) |
| `monitor` | [`../issue-looping-monitor/SKILL.md`](../issue-looping-monitor/SKILL.md) — signs `<!-- monitor -->`, not `<!-- owner -->` as it did before 2026-09-10 |
| unset | stop and ask the owner which role this session is |

**Do not pick a role skill because its description matched what you were asked
to do.** Your role comes from `CC_ROLE`. The shared skill's *Which role you are*
is the first thing to read and it explains why.

`references/subagent-brief.md` moved to
[`../issue-looping-shared/references/subagent-brief.md`](../issue-looping-shared/references/subagent-brief.md).

This directory goes away once all three running sessions have restarted onto the
new layout.
