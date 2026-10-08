# Multi-agent development

The framework runs four cooperating sessions: the reviewer implements changes
through worktree subagents, the coder reviews PR diffs and merges and deploys,
the monitor moderates the backlog, and the tester exercises the running app.
The aide handles tasks requested directly by the owner outside the loop.
Sessions coordinate through GitHub issues, labels, pull requests, and signed comments.

## Setup

Run from the root of your clone. Install and authenticate `gh`, and install the
CLI for each role: `agy` is the existing default for coder, reviewer, tester,
and aide; `claude` is the default for monitor. Python 3 and Bash are required.
App checks also require the dependencies installed with `uv sync`.

```bash
export SHARED_REPO_PATH="$(git rev-parse --show-toplevel)"
git config core.hooksPath .githooks
.claude/hooks/ccrole.sh cli
```

Use this same `SHARED_REPO_PATH` in each role terminal and inherited worktrees.
The scripts retain the original CLI and model defaults; check `ccrole.sh` and
set `CCROLE_CLI`, the per-role `CCROLE_*_CLI`, or `CCROLE_AGY_MODEL` for your
installed CLI and available model. The launcher passes
`--dangerously-skip-permissions`; role hooks still apply.

Read the [shared rules](../.claude/skills/issue-looping-shared/SKILL.md) and
[label setup](../.claude/skills/issue-looping-shared/references/labels.md) before
starting. Configure deployment targets and local credentials for your own app.
The inherited skills describe Pi deployment and local services; copying the
framework does not provision those services or start any agents.

Start each role in a separate terminal at the repository root:

```bash
.claude/hooks/ccloop.sh coder
.claude/hooks/ccloop.sh reviewer
.claude/hooks/ccloop.sh tester
.claude/hooks/ccrole.sh monitor
.claude/hooks/ccrole.sh aide
```

The monitor is interactive; give it the monitoring instruction described in its
skill. The aide runs only when the owner has a task for it. The optional
`.claude/hooks/ccwake.sh` watcher wakes loops when GitHub work changes.
The original `/tmp` log, worktree, and sweep paths are retained: do not run this
framework alongside the old workspace's loop on the same machine.

## Files and checks

`.claude/skills/issue-looping-*` contains the role instructions and briefs.
`.agents/skills/` links to those same files within this repository.
`.claude/hooks/` contains launchers, queue polling, role checks, and triage gates;
`.claude/settings.json` and `.agents/hooks.json` register them for their hosts.
`.githooks/` contains commit and push checks, enabled by the setup command above.
Host hook configuration must be verified in the CLI you use; it is not a
portable enforcement mechanism for every coding agent.

```bash
python3 .claude/hooks/role_guard_test.py
python3 .claude/hooks/triage_gate_test.py
python3 .claude/hooks/ccloop_test.py
```

See the [operating reference](../.claude/hooks/README.md) for role overrides,
backoff, and hook diagnostics. Historical issue numbers in the imported skills
refer to the original workspace; they are not issues in this repository.

This repository is public. Publish synthetic test data only. Keep credentials,
household screenshots, messages, calendars, and conversations out of issues,
comments, commits, and logs shared publicly.

The bundled configuration uses example hosts and accounts. Set `SDM_PROJECT_ID`
for Nest camera access, replace deployment hostnames and users, and record your
own room impulse responses if you run the acoustic benchmarks.
