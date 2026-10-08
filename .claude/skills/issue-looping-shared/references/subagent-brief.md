# Subagent brief — the invariant half

Paste everything below the rule into the top of every subagent brief, verbatim,
then write the issue-specific half underneath it. This file exists because the
constant part is about 80% of a brief, and retyping it from memory is how a
requirement gets dropped — on 2026-09-09 the brief for #71 lost the parent-SHA
sweep that way.

Adjust only the bracketed values. If a path here stops matching the repo, fix it
*here*, not in the brief you happen to be writing.

---

## Hard limits

You are read-only. Report back; do not act.

- **Nothing inside the repo changes because you ran.** No edits, no new files,
  no commits, no branches, no deletions — not source, not docs, not `.env`, not
  a scratch script you meant to clean up. The working tree belongs to another
  session and is dirty with its in-flight work.
- **`/tmp` is the only path you may write to**, and nothing comes back from it.
  Prefix every path `<your parent's role>-<issue number>-` — `rev-` under the
  reviewer, `mon-` under the monitor, matching the role prefixes in the shared
  skill. Both of those roles paste this file, so a hardcoded `rev-` would put a
  monitor's subagent in the reviewer's namespace and defeat the point of the
  prefix. The session that launched you names the prefix below; use that one.
  It matters because the other sessions are writing
  to `/tmp` too and reach for the same obvious names. The collision is not only
  between the three known sessions: on 2026-09-10 a fourth session that had
  inferred its role wrote to `/tmp/rev-rev-body.md`, the reviewer's prefix, and
  the namespace was already shared before anyone knew that session existed.
- **No GitHub writes.** No issues, no comments, no labels, no closes. You hand
  text back to the session that launched you and it publishes. A `role_guard.py`
  hook refuses these from a session with no role, but **you inherit your parent's
  `CC_ROLE`**, so the guard will wave you through. It cannot tell you apart from
  the session that launched you; this rule is the only thing that can.
- **Audit at a commit, never at the tree.** `git show <sha>:./path`. Redirect
  into `/tmp` if you need to run it.

### Three things you never run

| Never run | What it reaches |
|---|---|
| `tools/env_check.py` | Its last block probes with no `GOOGLE_APPLICATION_CREDENTIALS` set, so `google.auth` goes out to the GCE metadata server at `169.254.169.254` (`env_check.py:540-556`). Use `--except env` with `check_all.py` unless `.env` is symlinked. |
| `gws` binary or live Google Workspace calls | Running `gws` CLI or un-stubbed Workspace calls authenticates as the owner and reads real data. (All 22 check scripts in `tools/*_check.py`, including `timing_check.py` and `wiring_check.py`, stub external calls and are safe to run). |
| Anything that **writes** to the Raspberry Pi | copying files, starting/stopping/enabling a unit, **any `sudo` you typed yourself**, rebooting, editing its `.env`. Reading it is allowed, and `tools/pi_deploy.py check` is allowed even though it contains a `sudo` — see below. |

**Reading the Pi is allowed.** A read-only ssh — `systemctl --user is-active`,
`journalctl`, `loginctl`, `vcgencmd`, `cat` of a unit — settles claims that
would otherwise be guesses. `tools/pi_deploy.py check` does exactly this;
prefer it to hand-rolled commands.

**One of its probes runs `sudo i2cget` (`pi_deploy.py:673-674`), and that is
not a write.** What you are allowed to run is the reviewed probe set, not
`sudo` — a `sudo` you type yourself is still forbidden. The check plan being
write-free is asserted, not promised: `mutating()` (`:751-772`) reads the write
token off each command and `deploy_check.py`'s **`"nothing in the check plan
writes to the Pi"`** case fails the suite if any `check` step is mutating. Do not
stop when you see `sudo` in that output. (Cited by case name because the line
number drifted twice; grep for the string.)

**The host is `raspberrypi.local`, not `pi2`.** The bare name does not resolve from this
Mac. `pi_deploy.py`'s `resolve()` (`:1076-1091`) tries both, so a hand-rolled
`ssh` is the only way to hit this. If you see `Could not resolve hostname pi2`,
that is the wrong name — **not** an unreachable Pi, and not grounds for filing
the claim under *Not checkable from here*. Retry with `raspberrypi.local` before you
write anything off.

**One trap.** An ssh login is itself a state change when `Linger=no`: systemd
starts the user manager at first login, so `systemctl --user is-active` can
report `active` for units an untouched boot never started. Always probe
`loginctl show-user <user> -p Linger` alongside, and report it with the result —
`Linger=yes` makes the reading trustworthy, `Linger=no` makes it meaningless.

**Never probe during an acceptance window.** That means an unattended-boot test,
and it also means any window where a session is running an issue's acceptance
steps and will report what it saw — #10's reboot test is the standing example.
Ask before you ssh whether anyone is mid-run; on 2026-09-10 an agent probed
inside one because this line said *unattended-boot test* and the run in progress
was called something else.

**Beyond `gh` reads against this repo and read-only ssh to the Pi, no network.**
No model call, no fetch, no
package install, no live service. Everything you need is in the checkout, in the
installed packages under `.venv`, and in `/tmp`. A claim that genuinely needs
the network goes under *Not checkable from here*; that section is the release
valve and using it is not a failure.

**Never print a secret value.** `.env` holds real API keys. Checking that a
variable is set, or that its shape is right, is fine. Echoing the value is not —
not to the terminal, not into anything you hand back, not as an argument to a
command whose output you will quote.

**One live trap, and it is a read.** `GET /api/config` on the Pi returns
`maps_api_key` in the clear, because the browser needs it for Maps JS. So
`curl -s http://localhost:8000/api/config` over read-only ssh prints a real key
while looking like a health check.

**`jq` is not installed on pi2**, so the obvious `| jq 'del(.maps_api_key)'`
fails and `curl` has already printed the key by then — a redaction that fails
open, which is worse than none because you stop watching for it. Print only what
you asked for instead:

```bash
ssh raspberrypi.local 'curl -s localhost:8000/api/config | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(d[\"revision\"]); print(sorted(d))"'
```

**Better still, do not ask the endpoint.** If the question is *which build is
running*, `/api/config` answers it with a hand-written label (#155) while
carrying a real key. Compare content instead: `systemctl --user show ai-terminal
-p Environment` for the claimed sha, `sha256sum` the deployed
`static/css/style.css` on the Pi, and `git show <sha>:…` piped through
`shasum -a 256` on the Mac. Matching hashes are stronger evidence and touch no
secret.

Before you run anything over ssh, ask what its whole output contains, not just
what you are looking for in it — whether every tool in your pipe exists on that
host, and whether you need that command at all.

**Images are evidence — read them.** Owner's ruling, 2026-09-13. If the issue,
the thread or `/tmp` carries a screenshot of what failed, open it; a UX defect is
a defect in what a screen looks like, and a report written only from source is
arguing about a picture it has never seen. Hand any image path back to the
session that launched you along with the DOM text or log line it corroborates —
**you still publish nothing yourself**. Never a secret in an image: a
`maps_api_key` rendered on the settings sheet is a key, and unlike text it will
not turn up in a grep.

## Where things are

| | |
|---|---|
| repo | `${SHARED_REPO_PATH}` |
| project | `.` (all paths below are relative to it) |
| source | `src/ai_terminal/`, installed editable as `ai_terminal` |
| check scripts | `tools/*_check.py`, one per area, each printing `all N cases hold` |
| interpreter | `<repo>/.venv/bin/python` |

## Running the sweep at a SHA

```bash
D=/tmp/<role>-<N>-tree; mkdir -p $D    # rev- under the reviewer, mon- under the monitor
git archive <sha> | tar -x -C $D
cd $D

PY=${SHARED_REPO_PATH}/.venv/bin/python
export PYTHONPATH=$D/src                     # the venv's .pth points at the working tree
export PATH=/opt/homebrew/bin:$PATH          # js_check needs node
export PLAYWRIGHT_BROWSERS_PATH=$HOME/Library/Caches/ms-playwright

# Provenance. Must print a path under $D. Anything else and you are measuring
# the other session's working tree, not the commit. Report what it printed.
$PY -c "import ai_terminal, os; print(os.path.realpath(ai_terminal.__file__))"

# Run through check_all.py: all 22 scripts (or --except env if .env is unlinked).
# This MUST hold /tmp/rev-sweep.lock, and with the exact recipe in
# references/implementer-brief.md — read it and paste it, do not invent a lock.
# A lock of your own design shares the path with everyone else's and sees none
# of them, so it reads as caution and buys two concurrent sweeps (2026-09-12).
$PY tools/check_all.py --except env
```

**Four ways this silently measures the wrong thing.** All four exit 0 and
print a plausible number.

- **The venv imports the working tree.** Its `.pth` is an absolute path into
  `…/src`, so without `PYTHONPATH` the extracted SHA
  contributes only the check scripts. The provenance line is how you know.
- **A missing browser skips 12 cases without failing.** `browse_check.py:1338`
  and `:1397` catch any Chromium launch failure, print `skipped: no Chromium …`
  and carry on green. This produced a published `browse 187` against a true
  `199` on 2026-09-09.
- **A tool missing from `PATH` does the same.** A sweep subshell does not
  inherit an interactive shell's `PATH`.
- **`deploy_check.py` drops 4 cases when there is no surrounding git repo.** It
  shells out to `git ls-files --error-unmatch` (`:376`, `:383`); a `git archive`
  extraction has no repo, so it prints `skipped: git does not track …`, drops the
  cases, and **still exits 0**. The recipe above produces the no-repo variant by
  default. Measured at `f1b93cf`: 376 extracted, 380 inside a repo — and six
  different counts (371, 375, 376, 380, 386, 391) have been published for the
  same suite across setups. **Two counts from different setups are not
  comparable**, which is the whole reason to say which setup produced yours.

Report the per-script counts, not just the total, and flag any area whose count
moved in a direction the commit does not explain.

**Testing a patch uses the same repository layout.** Extract without stripping
path components, initialize the extracted repository, and inspect the diff
after applying the patch.

## The bar for a finding

**Only a behavioural defect counts as a gap**: wrong output, a regression, a
case the fix misses, data loss, or a claim someone would act on and be misled
by. Everything else — missing coverage on code that is correct today, comment
and docstring wording, `file:line` drift in prose, style, naming, structure —
goes in a separate `Also` section marked non-blocking, and does not change the
verdict.

**Architectural Intent (#629, #373)**: When an issue carries explicit owner directives or
architectural instructions, substituting a narrow regex or symptom patch while ignoring
the architectural directive is a behavioural gap.

If you cannot write the concrete failure (inputs, state, wrong output), it is
not behavioural. A mutation no check script catches is a coverage hole, not a
bug: report it under `Also`.

Do not default to maximal scrutiny. A `PARTIAL` over a docstring is a wrong
verdict, not a careful one.

## Every claim carries a `file:line`

At the audited SHA, not at the working tree — they differ, and citing the tree
is the most common way a report turns out to be wrong. For every gap, give the
root cause at `file:line` **and** a fix concrete enough to type: the file, the
line, and the new code or a precise description of it. "Handle this better" is
not a finding.

**Check the premises.** Anything your conclusion rests on about a library's
behaviour gets checked against the *installed* package source under `.venv`,
with the version and a `file:line`. Issue bodies and closing comments are
written from memory; a wrong premise caught here saves a day.

Separate what you ran from what you inferred. Say plainly what you could not
settle.

## Mandatory collation check for analysis and filing

When your task is triage analysis or filing preparation, **you must collate
against open issues before producing output (#199)**.

1. **Run the collation query** for the primary symbol or function name:
   ```bash
   gh api "repos/:owner/:repo/issues?state=open&labels=ai-terminal&per_page=100" \
     -q '.[] | select(.pull_request|not)
             | select((.title + " " + .body) | test("<primary-symbol>"; "i"))
             | "#\(.number)\t\(.title)"'
   ```
2. **Include the mandatory `Collation:` token** at the top of your draft:
   - If matching issues exist:
     `Collation: checked open issues for '<symbol>'. Matches: #N. Relation: <distinct / supersedes / duplicate>...`
   - If no matching issues exist:
     `Collation: checked open issues for '<symbol>', none found.`
   **Never omit the token.** An empty result must be recorded explicitly as
   `none found`.

When your task is **claim-check** of an issue draft or triage analysis:
- Assert that the `Collation:` token exists and names the primary symbol.
- If omitted or blank, return `REFUTED: missing mandatory collation token`.
