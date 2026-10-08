#!/usr/bin/env python3
"""Unit tests for ccloop.sh and GitHub heartbeat contracts (#571).

Run it: `uv run --no-project python ccloop_test.py`.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
CCLOOP = HERE / "ccloop.sh"
SHARED_SKILL = HERE.parent / "skills" / "issue-looping-shared" / "SKILL.md"
REVIEWER_SKILL = HERE.parent / "skills" / "issue-looping-reviewer" / "SKILL.md"
MONITOR_SKILL = HERE.parent / "skills" / "issue-looping-monitor" / "SKILL.md"
TESTER_SKILL = HERE.parent / "skills" / "issue-looping-tester" / "SKILL.md"

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     want: {want!r}\n      got: {got!r}")


def test_syntax_and_executable() -> None:
    check("ccloop.sh exists", CCLOOP.is_file(), True)
    check("ccloop.sh is executable", os.access(CCLOOP, os.X_OK), True)
    res = subprocess.run(["bash", "-n", str(CCLOOP)], capture_output=True, text=True)
    check("ccloop.sh syntax is valid", res.returncode, 0)


def test_invalid_role() -> None:
    res = subprocess.run(
        [str(CCLOOP), "invalid_role"],
        capture_output=True,
        text=True,
    )
    check("ccloop.sh invalid_role exits non-zero", res.returncode != 0, True)
    check(
        "ccloop.sh invalid_role error message",
        "unknown role 'invalid_role'" in res.stderr,
        True,
    )


def test_ccloop_configuration_and_role_defaults() -> None:
    content = CCLOOP.read_text(encoding="utf-8")

    # Header comments
    check("ccloop.sh documents RUNNING THE REVIEWER", "RUNNING THE REVIEWER" in content, True)
    check("ccloop.sh documents /tmp/rev-triage-gate persistence", "/tmp/rev-triage-gate" in content, True)
    check("ccloop.sh documents LOOP-REVIEWER-IDLE sentinel", "LOOP-REVIEWER-IDLE" in content, True)
    check("ccloop.sh documents RUNNING THE TESTER", "RUNNING THE TESTER" in content, True)
    check("ccloop.sh documents LOOP-TESTER-IDLE sentinel", "LOOP-TESTER-IDLE" in content, True)

    # Defaults
    check("ccloop.sh supports ROLE=reviewer", 'ROLE="${ROLE:-coder}"' in content, True)
    check("ccloop.sh sets reviewer LOGDIR to /tmp/rev-loop", 'LOGDIR="${LOGDIR:-/tmp/rev-loop}"' in content, True)
    check("ccloop.sh sets reviewer IDLE_GAP to 270", 'IDLE_GAP="${IDLE_GAP:-270}"' in content, True)
    check("ccloop.sh sets reviewer IDLE_GAP_MAX to 1800", 'IDLE_GAP_MAX="${IDLE_GAP_MAX:-1800}"' in content, True)
    check("ccloop.sh defines PROMPT_REVIEWER", "PROMPT_REVIEWER" in content, True)
    check("ccloop.sh sets tester LOGDIR to /tmp/tst-loop", 'LOGDIR="${LOGDIR:-/tmp/tst-loop}"' in content, True)
    check("ccloop.sh sets tester IDLE_GAP to 1200", 'IDLE_GAP="${IDLE_GAP:-1200}"' in content, True)
    check("ccloop.sh sets tester IDLE_GAP_MAX to 1800", 'IDLE_GAP_MAX="${IDLE_GAP_MAX:-1800}"' in content, True)
    check("ccloop.sh sets tester IDLE_SENTINEL to LOOP-TESTER-IDLE", 'IDLE_SENTINEL="LOOP-TESTER-IDLE"' in content, True)
    check("ccloop.sh defines PROMPT_TESTER", "PROMPT_TESTER" in content, True)
    check("ccloop.sh defines progress_snapshot", "progress_snapshot()" in content, True)
    check("ccloop.sh sets START_FAIL_MAX default to 3", 'START_FAIL_MAX="${START_FAIL_MAX:-3}"' in content, True)
    check("ccloop.sh sets START_RETRY_GAP default to 10", 'START_RETRY_GAP="${START_RETRY_GAP:-10}"' in content, True)
    check("ccloop.sh sets COUNTDOWN_SLEEP default to 1", 'COUNTDOWN_SLEEP="${COUNTDOWN_SLEEP:-1}"' in content, True)



def test_execution_with_stub_ccrole() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = pathlib.Path(tmpdir)
        stub_ccrole = tmp_path / "stub_ccrole.sh"
        stub_log = tmp_path / "stub_invocations.txt"

        stub_ccrole.write_text(
            f"""#!/usr/bin/env bash
role="$1"
shift
echo "role:$role" >> "{stub_log}"
if [ "$role" = "reviewer" ]; then
  echo "Reviewer tick executed"
  echo "LOOP-REVIEWER-IDLE"
elif [ "$role" = "tester" ]; then
  echo "Tester tick executed"
  echo "LOOP-TESTER-IDLE"
elif [ "$role" = "coder" ]; then
  echo "Coder batch executed"
  echo "LOOP-NOTHING-TO-MERGE"
fi
exit 0
""",
            encoding="utf-8",
        )
        stub_ccrole.chmod(0o755)

        # 1. Run coder with MAX=1
        env_coder = dict(os.environ)
        env_coder["CCROLE"] = str(stub_ccrole)
        env_coder["LOGDIR"] = str(tmp_path / "cod_logs")
        env_coder["MAX"] = "1"
        env_coder["STREAM"] = "0"
        env_coder["IDLE_GAP"] = "0"
        env_coder["GAP"] = "0"
        env_coder["NO_FETCH"] = "1"
        env_coder["PROGRESS_SNAPSHOT"] = "snap-coder"

        res_coder = subprocess.run(
            [str(CCLOOP), "coder"],
            capture_output=True,
            text=True,
            env=env_coder,
            timeout=10,
        )
        check("ccloop.sh coder with stub exits 0", res_coder.returncode, 0)

        # 2. Run reviewer with MAX=1
        env_rev = dict(os.environ)
        env_rev["CCROLE"] = str(stub_ccrole)
        env_rev["LOGDIR"] = str(tmp_path / "rev_logs")
        env_rev["MAX"] = "1"
        env_rev["STREAM"] = "0"
        env_rev["IDLE_GAP"] = "0"
        env_rev["GAP"] = "0"
        env_rev["PROGRESS_SNAPSHOT"] = "snap-reviewer"

        res_rev = subprocess.run(
            [str(CCLOOP), "reviewer"],
            capture_output=True,
            text=True,
            env=env_rev,
            timeout=10,
        )
        check("ccloop.sh reviewer with stub exits 0", res_rev.returncode, 0)

        # 3. Run tester with MAX=1
        env_tst = dict(os.environ)
        env_tst["CCROLE"] = str(stub_ccrole)
        env_tst["LOGDIR"] = str(tmp_path / "tst_logs")
        env_tst["MAX"] = "1"
        env_tst["STREAM"] = "0"
        env_tst["IDLE_GAP"] = "0"
        env_tst["GAP"] = "0"
        env_tst["PROGRESS_SNAPSHOT"] = "snap-tester"

        res_tst = subprocess.run(
            [str(CCLOOP), "tester"],
            capture_output=True,
            text=True,
            env=env_tst,
            timeout=10,
        )
        check("ccloop.sh tester with stub exits 0", res_tst.returncode, 0)

        # Check invocations
        invocations = stub_log.read_text(encoding="utf-8").splitlines()
        check("stub invocations count", len(invocations), 3)
        check("stub invocation 1 role", invocations[0], "role:coder")
        check("stub invocation 2 role", invocations[1], "role:reviewer")
        check("stub invocation 3 role", invocations[2], "role:tester")

        # Check logs and idle sentinels
        coder_raw = (tmp_path / "cod_logs" / "001.log").read_text(encoding="utf-8")
        check("coder log has LOOP-NOTHING-TO-MERGE", "LOOP-NOTHING-TO-MERGE" in coder_raw, True)
        rev_raw = (tmp_path / "rev_logs" / "001.log").read_text(encoding="utf-8")
        check("reviewer log has LOOP-REVIEWER-IDLE", "LOOP-REVIEWER-IDLE" in rev_raw, True)
        tst_raw = (tmp_path / "tst_logs" / "001.log").read_text(encoding="utf-8")
        check("tester log has LOOP-TESTER-IDLE", "LOOP-TESTER-IDLE" in tst_raw, True)


test_ccloop_driver_roles = test_execution_with_stub_ccrole


def test_heartbeat_contract_definitions() -> None:
    shared_text = SHARED_SKILL.read_text(encoding="utf-8")
    reviewer_text = REVIEWER_SKILL.read_text(encoding="utf-8")
    monitor_text = MONITOR_SKILL.read_text(encoding="utf-8")

    metric_pat = re.compile(r"the interval between a role publishing a waiting condition and that\s+role acting again")
    check("shared skill defines heartbeat metric", bool(metric_pat.search(shared_text)), True)
    check("monitor skill tests heartbeat metric", bool(metric_pat.search(monitor_text)), True)

    check("shared skill has heartbeat marker syntax", "<!-- heartbeat" in shared_text, True)
    check("reviewer skill documents ccloop.sh per-tick driver", "Per-tick external driver (`ccloop.sh`, preferred)" in reviewer_text, True)
    check("reviewer skill documents /tmp/rev-triage-gate persistence", "/tmp/rev-triage-gate" in reviewer_text, True)
    check("reviewer skill documents prompt-cache tradeoff", "Prompt-cache tradeoff" in reviewer_text, True)


def test_heartbeat_marker_parsing() -> None:
    pattern = re.compile(
        r"<!--\s*heartbeat\s+role=(?P<role>[a-z]+)\s+state=(?P<state>[a-z_]+)"
        r"(?:\s+waiting_for=(?P<waiting_for>[a-z_]+))?"
        r"(?:\s+since=(?P<since>[^\s>]+))?"
        r"\s*-->"
    )

    m1 = pattern.search("<!-- heartbeat role=reviewer state=waiting_subagents since=2026-09-14T09:00:00Z -->")
    check("marker 1 matches", m1 is not None, True)
    if m1:
        check("marker 1 role", m1.group("role"), "reviewer")
        check("marker 1 state", m1.group("state"), "waiting_subagents")
        check("marker 1 since", m1.group("since"), "2026-09-14T09:00:00Z")

    m2 = pattern.search("<!-- heartbeat role=coder state=idle -->")
    check("marker 2 matches", m2 is not None, True)
    if m2:
        check("marker 2 role", m2.group("role"), "coder")
        check("marker 2 state", m2.group("state"), "idle")


def test_heartbeat_stall_thresholds() -> None:
    # Reviewer subagent in-flight stall threshold: 45 min = 2700s
    def is_subagent_stalled(elapsed_seconds: float) -> bool:
        return elapsed_seconds > 2700

    check("subagent 20m in-flight is not stalled", is_subagent_stalled(20 * 60), False)
    check("subagent 45m in-flight is not stalled", is_subagent_stalled(45 * 60), False)
    check("subagent 46m in-flight is stalled", is_subagent_stalled(46 * 60), True)

    # Reviewer idle with actionable work threshold: 60 min = 3600s
    def is_actionable_idle_stalled(elapsed_seconds: float, has_actionable_work: bool) -> bool:
        return has_actionable_work and elapsed_seconds > 3600

    check("idle 50m with work is not stalled", is_actionable_idle_stalled(50 * 60, True), False)
    check("idle 61m with work is stalled", is_actionable_idle_stalled(61 * 60, True), True)
    check("idle 90m without work is not stalled", is_actionable_idle_stalled(90 * 60, False), False)


def test_idle_sentinel_triage_drain_constraint() -> None:
    ccloop_text = CCLOOP.read_text(encoding="utf-8")
    reviewer_text = REVIEWER_SKILL.read_text(encoding="utf-8")

    check(
        "ccloop.sh mentions all 4 triggers in triage gate prompt",
        all(f"{i}:" in ccloop_text or f"Trigger {i}" in ccloop_text for i in (1, 2, 3, 4)),
        True,
    )
    drain_constraint_pat = re.compile(
        r"cannot (?:be printed|print)\s+while\s+(?:the\s+)?triage drain query is non-empty"
    )
    check(
        "ccloop.sh enforces idle sentinel cannot print while triage drain non-empty",
        bool(drain_constraint_pat.search(ccloop_text)),
        True,
    )
    check(
        "reviewer skill enforces idle sentinel cannot print while triage drain non-empty",
        bool(drain_constraint_pat.search(reviewer_text)),
        True,
    )


def test_tester_prompt_and_idle_sentinel_stage_live_constraint() -> None:
    ccloop_text = CCLOOP.read_text(encoding="utf-8")
    tester_text = TESTER_SKILL.read_text(encoding="utf-8")

    prompt_match = re.search(r"read -r -d '' PROMPT_TESTER <<EOF(.*?)EOF", ccloop_text, re.DOTALL)
    check("ccloop.sh defines PROMPT_TESTER block", prompt_match is not None, True)
    if prompt_match:
        prompt = prompt_match.group(1)
        check("PROMPT_TESTER contains stage:live queue step", "stage:live" in prompt, True)
        check("PROMPT_TESTER specifies raspberrypi.local verification", "raspberrypi.local" in prompt, True)
        check("PROMPT_TESTER specifies live-verified: marker", "live-verified:" in prompt, True)

        live_pos = prompt.find("stage:live")
        sweep_pos = prompt.find("Sweep the next five closed issues off the backlog")
        check(
            "PROMPT_TESTER orders stage:live before backlog sweep",
            0 <= live_pos < sweep_pos if sweep_pos != -1 else False,
            True,
        )

        sentinel_constraint_pat = re.compile(
            r"LOOP-TESTER-IDLE\s+cannot (?:be printed|print)\s+while\s+(?:any\s+)?stage:live issue\s+lacks\s+a\s+tester\s+comment"
        )
        check(
            "ccloop.sh enforces idle sentinel cannot print while stage:live unverified",
            bool(sentinel_constraint_pat.search(prompt)),
            True,
        )

    tester_sentinel_pat = re.compile(
        r"LOOP-TESTER-IDLE`?\s+cannot (?:be printed|print)\s+while\s+(?:any\s+)?`?stage:live`?\s+issue\s+lacks\s+a\s+tester\s+comment"
    )
    check(
        "tester skill enforces idle sentinel cannot print while stage:live unverified",
        bool(tester_sentinel_pat.search(tester_text)),
        True,
    )


def test_print_timeout_and_stranded_work_rescue() -> None:
    ccloop_text = CCLOOP.read_text(encoding="utf-8")
    check(
        "reviewer prompt has budget rule",
        "4. Budget: You have a hard wall-clock timeout of $TIMEOUT for this tick." in ccloop_text,
        True,
    )
    check(
        "tester prompt has budget rule",
        "4. Budget: You have a hard wall-clock timeout of $TIMEOUT for this tick." in ccloop_text,
        True,
    )

    test_issue_num = 998877
    test_wt = pathlib.Path(f"/tmp/rev-{test_issue_num}-wt")
    rescued_diff = pathlib.Path(f"/tmp/{test_wt.name}-rescued.diff")
    try:
        test_wt.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=str(test_wt), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(test_wt), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(test_wt), check=True, capture_output=True)
        tracked_file = test_wt / "feature.py"
        tracked_file.write_text("initial code\n", encoding="utf-8")
        subprocess.run(["git", "add", "feature.py"], cwd=str(test_wt), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(test_wt), check=True, capture_output=True)
        tracked_file.write_text("initial code\nstranded patch line\n", encoding="utf-8")

        untracked_file = test_wt / "untracked.py"
        untracked_file.write_text("print('rescued untracked file')\n", encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = pathlib.Path(tmpdir)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            stub_gh = bin_dir / "gh"
            gh_log = tmp_path / "gh_calls.jsonl"
            stub_gh.write_text(
                f"""#!/usr/bin/env python3
import json, sys
with open({str(gh_log)!r}, "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(0)
""",
                encoding="utf-8",
            )
            stub_gh.chmod(0o755)

            stub_ccrole = tmp_path / "stub_timeout_ccrole.sh"
            stub_ccrole.write_text(
                """#!/usr/bin/env bash
echo "print timeout after 30m0s with turn in progress; returning partial output" >&2
exit 1
""",
                encoding="utf-8",
            )
            stub_ccrole.chmod(0o755)

            log_dir = tmp_path / "timeout_logs"
            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["CCROLE"] = str(stub_ccrole)
            env["LOGDIR"] = str(log_dir)
            env["MAX"] = "1"
            env["STREAM"] = "0"
            env["IDLE_GAP"] = "0"
            env["GAP"] = "0"
            env["NO_FETCH"] = "1"
            env["TIMEOUT"] = "30m"
            env["PROGRESS_SNAPSHOT"] = "snap-timeout"

            res = subprocess.run(
                [str(CCLOOP), "reviewer"],
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
            )

            check("ccloop timeout run exits 0 on MAX=1", res.returncode, 0)
            check("stderr detects print timeout message", "!!! tick timed out after 30m" in res.stderr, True)
            check(
                "stderr reports rescued worktree diff",
                f"!!! Rescued stranded work from {test_wt} into {rescued_diff}" in res.stderr,
                True,
            )
            check("stdout or countdown shows agy timed out (30m)", "agy timed out (30m)" in res.stdout, True)

            iter_log = log_dir / "001.log"
            check("iteration log exists", iter_log.is_file(), True)
            if iter_log.is_file():
                log_content = iter_log.read_text(encoding="utf-8")
                check("log has tick timed out message", "!!! tick timed out after 30m" in log_content, True)
                check(
                    "log has rescued stranded work message",
                    f"!!! Rescued stranded work from {test_wt} into {rescued_diff}" in log_content,
                    True,
                )

            check("rescued diff file exists", rescued_diff.is_file(), True)
            if rescued_diff.is_file():
                diff_content = rescued_diff.read_text(encoding="utf-8")
                check("rescued diff contains stranded patch line", "stranded patch line" in diff_content, True)
                check("rescued diff contains untracked file header", "untracked.py" in diff_content, True)
                check("rescued diff contains untracked file content", "rescued untracked file" in diff_content, True)

            check("gh calls log exists", gh_log.is_file(), True)
            if gh_log.is_file():
                calls = [json.loads(line) for line in gh_log.read_text(encoding="utf-8").splitlines() if line.strip()]
                issue_calls = [
                    c for c in calls
                    if c and len(c) >= 5 and c[0] == "issue" and c[1] == "comment" and c[2] == str(test_issue_num)
                ]
                check("gh issue comment called for test issue", len(issue_calls) > 0, True)
                if issue_calls:
                    target_call = issue_calls[0]
                    check("gh issue comment flag is --body", target_call[3], "--body")
                    body = target_call[4]
                    check("gh comment body starts with <!-- reviewer -->", body.startswith("<!-- reviewer -->"), True)
                    check("gh comment body mentions timeout", "Tick timed out after 30m with uncommitted work." in body, True)
                    check("gh comment body references patch file", f"Rescued patch saved to `{rescued_diff}` on loop host." in body, True)
    finally:
        if test_wt.exists():
            shutil.rmtree(test_wt, ignore_errors=True)
        if rescued_diff.exists():
            rescued_diff.unlink(missing_ok=True)
        pathlib.Path("/tmp/rev-670-wt-rescued.diff").unlink(missing_ok=True)


def test_timeout_rescue_on_elapsed_time_without_stderr_message() -> None:
    test_issue_num = 998879
    test_wt = pathlib.Path(f"/tmp/rev-{test_issue_num}-wt")
    rescued_diff = pathlib.Path(f"/tmp/{test_wt.name}-rescued.diff")
    try:
        test_wt.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=str(test_wt), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(test_wt), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(test_wt), check=True, capture_output=True)
        tracked_file = test_wt / "feature.py"
        tracked_file.write_text("initial code\n", encoding="utf-8")
        subprocess.run(["git", "add", "feature.py"], cwd=str(test_wt), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(test_wt), check=True, capture_output=True)
        tracked_file.write_text("initial code\nstranded patch elapsed\n", encoding="utf-8")

        untracked_file = test_wt / "untracked.py"
        untracked_file.write_text("print('rescued untracked file elapsed')\n", encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = pathlib.Path(tmpdir)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            stub_gh = bin_dir / "gh"
            gh_log = tmp_path / "gh_calls.jsonl"
            stub_gh.write_text(
                f"""#!/usr/bin/env python3
import json, sys
with open({str(gh_log)!r}, "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(0)
""",
                encoding="utf-8",
            )
            stub_gh.chmod(0o755)

            stub_ccrole = tmp_path / "stub_elapsed_ccrole.sh"
            stub_ccrole.write_text(
                """#!/usr/bin/env bash
echo "=== role banner ===" >&2
sleep 1
exit 0
""",
                encoding="utf-8",
            )
            stub_ccrole.chmod(0o755)

            log_dir = tmp_path / "timeout_elapsed_logs"
            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["CCROLE"] = str(stub_ccrole)
            env["LOGDIR"] = str(log_dir)
            env["MAX"] = "1"
            env["STREAM"] = "0"
            env["IDLE_GAP"] = "0"
            env["GAP"] = "0"
            env["NO_FETCH"] = "1"
            env["TIMEOUT"] = "1s"
            env["PROGRESS_SNAPSHOT"] = "snap-elapsed-timeout"

            res = subprocess.run(
                [str(CCLOOP), "reviewer"],
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
            )

            check("ccloop elapsed timeout run exits 0 on MAX=1", res.returncode, 0)
            check("stderr detects tick timed out message on elapsed time", "!!! tick timed out after 1s" in res.stderr, True)
            check(
                "stderr reports rescued worktree diff on elapsed time",
                f"!!! Rescued stranded work from {test_wt} into {rescued_diff}" in res.stderr,
                True,
            )
            check("stdout or countdown shows agy timed out (1s)", "agy timed out (1s)" in res.stdout, True)

            iter_log = log_dir / "001.log"
            check("iteration log exists for elapsed timeout", iter_log.is_file(), True)
            if iter_log.is_file():
                log_content = iter_log.read_text(encoding="utf-8")
                check("log has tick timed out message on elapsed time", "!!! tick timed out after 1s" in log_content, True)
                check(
                    "log has rescued stranded work message on elapsed time",
                    f"!!! Rescued stranded work from {test_wt} into {rescued_diff}" in log_content,
                    True,
                )

            check("rescued diff file exists on elapsed timeout", rescued_diff.is_file(), True)
            if rescued_diff.is_file():
                diff_content = rescued_diff.read_text(encoding="utf-8")
                check("rescued diff contains stranded patch line", "stranded patch elapsed" in diff_content, True)
                check("rescued diff contains untracked file header", "untracked.py" in diff_content, True)
                check("rescued diff contains untracked file content", "rescued untracked file elapsed" in diff_content, True)

            check("gh calls log exists on elapsed timeout", gh_log.is_file(), True)
            if gh_log.is_file():
                calls = [json.loads(line) for line in gh_log.read_text(encoding="utf-8").splitlines() if line.strip()]
                issue_calls = [
                    c for c in calls
                    if c and len(c) >= 5 and c[0] == "issue" and c[1] == "comment" and c[2] == str(test_issue_num)
                ]
                check("gh issue comment called for elapsed test issue", len(issue_calls) > 0, True)
                if issue_calls:
                    target_call = issue_calls[0]
                    check("gh issue comment flag is --body", target_call[3], "--body")
                    body = target_call[4]
                    check("gh comment body starts with <!-- reviewer -->", body.startswith("<!-- reviewer -->"), True)
                    check("gh comment body mentions timeout", "Tick timed out after 1s with uncommitted work." in body, True)
                    check("gh comment body references patch file", f"Rescued patch saved to `{rescued_diff}` on loop host." in body, True)
    finally:
        if test_wt.exists():
            shutil.rmtree(test_wt, ignore_errors=True)
        if rescued_diff.exists():
            rescued_diff.unlink(missing_ok=True)


def test_timeout_rescue_scoped_to_reviewer_role() -> None:
    test_issue_num = 998880
    test_wt = pathlib.Path(f"/tmp/rev-{test_issue_num}-wt")
    rescued_diff = pathlib.Path(f"/tmp/{test_wt.name}-rescued.diff")
    try:
        test_wt.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=str(test_wt), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(test_wt), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(test_wt), check=True, capture_output=True)
        tracked_file = test_wt / "feature.py"
        tracked_file.write_text("initial code\n", encoding="utf-8")
        subprocess.run(["git", "add", "feature.py"], cwd=str(test_wt), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(test_wt), check=True, capture_output=True)
        tracked_file.write_text("initial code\nuncommitted implementer work\n", encoding="utf-8")
        untracked_file = test_wt / "new_module.py"
        untracked_file.write_text("print('implementer uncommitted new module')\n", encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = pathlib.Path(tmpdir)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            stub_gh = bin_dir / "gh"
            gh_log = tmp_path / "gh_calls.jsonl"
            stub_gh.write_text(
                f"""#!/usr/bin/env python3
import json, sys
with open({str(gh_log)!r}, "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(0)
""",
                encoding="utf-8",
            )
            stub_gh.chmod(0o755)

            stub_ccrole = tmp_path / "stub_timeout_ccrole.sh"
            stub_ccrole.write_text(
                """#!/usr/bin/env bash
echo "print timeout after 30m0s with turn in progress; returning partial output" >&2
exit 1
""",
                encoding="utf-8",
            )
            stub_ccrole.chmod(0o755)

            log_dir = tmp_path / "tester_timeout_logs"
            env = dict(os.environ)
            path_val = env.get("PATH", "")
            env["PATH"] = f"{bin_dir}:{path_val}"
            env["CCROLE"] = str(stub_ccrole)
            env["LOGDIR"] = str(log_dir)
            env["MAX"] = "1"
            env["STREAM"] = "0"
            env["IDLE_GAP"] = "0"
            env["GAP"] = "0"
            env["NO_FETCH"] = "1"
            env["TIMEOUT"] = "30m"
            env["PROGRESS_SNAPSHOT"] = "snap-tester-timeout"

            res = subprocess.run(
                [str(CCLOOP), "tester"],
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
            )

            check("tester timeout run exits 0 on MAX=1", res.returncode, 0)
            check("tester stderr detects timeout message", "!!! tick timed out after 30m" in res.stderr, True)
            check(
                "tester stderr does not rescue reviewer worktree",
                f"!!! Rescued stranded work from {test_wt}" not in res.stderr,
                True,
            )
            check("tester timeout does not create rescued diff file", rescued_diff.exists(), False)

            if gh_log.is_file():
                calls = [json.loads(line) for line in gh_log.read_text(encoding="utf-8").splitlines() if line.strip()]
                issue_calls = [
                    c for c in calls
                    if c and len(c) >= 5 and c[0] == "issue" and c[1] == "comment" and c[2] == str(test_issue_num)
                ]
                check("tester timeout posts no issue comment on reviewer worktree", len(issue_calls), 0)
    finally:
        if test_wt.exists():
            shutil.rmtree(test_wt, ignore_errors=True)
        if rescued_diff.exists():
            rescued_diff.unlink(missing_ok=True)


def test_normal_exit_without_timeout() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = pathlib.Path(tmpdir)
        stub_ccrole = tmp_path / "stub_error_ccrole.sh"
        stub_ccrole.write_text(
            """#!/usr/bin/env bash
echo "fatal error: auth failed" >&2
exit 2
""",
            encoding="utf-8",
        )
        stub_ccrole.chmod(0o755)

        log_dir = tmp_path / "err_logs"
        env = dict(os.environ)
        env["CCROLE"] = str(stub_ccrole)
        env["LOGDIR"] = str(log_dir)
        env["MAX"] = "1"
        env["STREAM"] = "0"
        env["IDLE_GAP"] = "0"
        env["GAP"] = "0"
        env["NO_FETCH"] = "1"
        env["PROGRESS_SNAPSHOT"] = "snap-err"

        env["COUNTDOWN_SLEEP"] = "0"
        env["START_RETRY_GAP"] = "0"

        res = subprocess.run(
            [str(CCLOOP), "reviewer"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )

        check("ccloop error run exits 0 on MAX=1", res.returncode, 0)
        check("stdout reports agy exited 2", "agy exited 2" in res.stdout, True)
        check("stdout shows agy exited 2 message", "!!! agy exited 2 on iteration 1" in res.stdout, True)
        check("stderr does not report timeout", "tick timed out" not in res.stderr, True)


def test_transient_startup_failure_fast_retry() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = pathlib.Path(tmpdir)
        stub_ccrole = tmp_path / "stub_flake_ccrole.sh"
        invocations_file = tmp_path / "invocations.txt"
        stub_ccrole.write_text(
            f"""#!/usr/bin/env bash
count=0
if [ -f "{invocations_file}" ]; then
  count=$(cat "{invocations_file}")
fi
count=$((count + 1))
echo "$count" > "{invocations_file}"

if [ "$count" -eq 1 ]; then
  echo "auth token expired" >&2
  exit 1
fi

echo "Coder batch executed"
echo "LOOP-NOTHING-TO-MERGE"
exit 0
""",
            encoding="utf-8",
        )
        stub_ccrole.chmod(0o755)

        log_dir = tmp_path / "fast_retry_logs"
        env = dict(os.environ)
        env["CCROLE"] = str(stub_ccrole)
        env["LOGDIR"] = str(log_dir)
        env["MAX"] = "2"
        env["STREAM"] = "0"
        env["IDLE_GAP"] = "100"
        env["START_RETRY_GAP"] = "5"
        env["COUNTDOWN_SLEEP"] = "0"
        env["GAP"] = "0"
        env["NO_FETCH"] = "1"
        env["PROGRESS_SNAPSHOT"] = "snap-flake"

        res = subprocess.run(
            [str(CCLOOP), "coder"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )

        check("transient failure run exits 0 on MAX=2", res.returncode, 0)
        check("transient failure triggers fast retry warning", "fast retry 1/3" in res.stdout, True)
        check("transient failure does not create status file", (log_dir / "status").exists(), False)

        # Failure details must reach 001.log
        iter1_log = log_dir / "001.log"
        check("iter 1 log exists", iter1_log.is_file(), True)
        if iter1_log.is_file():
            iter1_content = iter1_log.read_text(encoding="utf-8")
            check("iter 1 log contains fast retry warning", "fast retry 1/3" in iter1_content, True)
            check("iter 1 log contains stderr details", "auth token expired" in iter1_content, True)

        # Iteration 2 succeeded and was idle: idle backoff should use original IDLE_GAP (100), not doubled
        check("fast retry does not invoke back_off", "agy exited 1 — next try at" not in res.stdout, True)
        check("idle sentinel backs off after iteration 2", "nothing to merge (1 in a row)" in res.stdout, True)


def test_persistent_startup_failure_handling() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = pathlib.Path(tmpdir)
        stub_ccrole = tmp_path / "stub_persistent_ccrole.sh"
        stub_ccrole.write_text(
            """#!/usr/bin/env bash
echo "fatal: persistent auth failure" >&2
exit 2
""",
            encoding="utf-8",
        )
        stub_ccrole.chmod(0o755)

        log_dir = tmp_path / "persistent_err_logs"
        env = dict(os.environ)
        env["CCROLE"] = str(stub_ccrole)
        env["LOGDIR"] = str(log_dir)
        env["START_FAIL_MAX"] = "2"
        env["MAX"] = "3"
        env["STREAM"] = "0"
        env["IDLE_GAP"] = "50"
        env["START_RETRY_GAP"] = "5"
        env["COUNTDOWN_SLEEP"] = "0"
        env["GAP"] = "0"
        env["NO_FETCH"] = "1"
        env["PROGRESS_SNAPSHOT"] = "snap-persistent"

        res = subprocess.run(
            [str(CCLOOP), "reviewer"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )

        check("persistent failure run exits 0 on MAX=3", res.returncode, 0)
        status_file = log_dir / "status"
        check("persistent failure creates status file", status_file.is_file(), True)
        if status_file.is_file():
            status_text = status_file.read_text(encoding="utf-8")
            check("status file contains error", "error:" in status_text, True)
            check("status file mentions agy exited 2", "agy exited 2" in status_text, True)
            check("status file mentions persistent failures", "persistent: 2 failures" in status_text, True)

        check("stdout reports persistent startup failure", "persistent startup failure (2 failures)" in res.stdout, True)
        check("stdout shows persistent backoff message", "agy exited 2 (persistent: 2 failures)" in res.stdout, True)

        iter3_log = log_dir / "003.log"
        check("iter 3 log exists", iter3_log.is_file(), True)
        if iter3_log.is_file():
            iter3_content = iter3_log.read_text(encoding="utf-8")
            check("iter 3 log contains persistent failure message", "persistent startup failure" in iter3_content, True)
            check("iter 3 log contains stderr details", "fatal: persistent auth failure" in iter3_content, True)


def test_success_clears_status_and_resets_start_fails() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = pathlib.Path(tmpdir)
        log_dir = tmp_path / "clear_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        status_file = log_dir / "status"
        status_file.write_text("error: previous persistent failure\n", encoding="utf-8")

        stub_ccrole = tmp_path / "stub_clear_ccrole.sh"
        stub_ccrole.write_text(
            """#!/usr/bin/env bash
echo "LOOP-NOTHING-TO-MERGE"
exit 0
""",
            encoding="utf-8",
        )
        stub_ccrole.chmod(0o755)

        env = dict(os.environ)
        env["CCROLE"] = str(stub_ccrole)
        env["LOGDIR"] = str(log_dir)
        env["MAX"] = "1"
        env["STREAM"] = "0"
        env["IDLE_GAP"] = "0"
        env["COUNTDOWN_SLEEP"] = "0"
        env["GAP"] = "0"
        env["NO_FETCH"] = "1"
        env["PROGRESS_SNAPSHOT"] = "snap-clear"

        res = subprocess.run(
            [str(CCLOOP), "coder"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )

        check("success run exits 0", res.returncode, 0)
        check("status file is removed on success", status_file.exists(), False)


CCWAKE = HERE / "ccwake.sh"
CCWAKE_SIG = HERE / "ccwake_sig.py"


def _stream(events: list[dict]) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


def _run_stream_tick(tmp_path: pathlib.Path, role: str, stream: str) -> subprocess.CompletedProcess:
    """One ccloop iteration whose stub emits `stream` as agy's stream-json."""
    payload = tmp_path / "payload.jsonl"
    payload.write_text(stream, encoding="utf-8")
    stub = tmp_path / "stub_stream_ccrole.sh"
    stub.write_text(f'#!/usr/bin/env bash\ncat "{payload}"\n', encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ)
    env.update(
        CCROLE=str(stub),
        LOGDIR=str(tmp_path / "logs"),
        MAX="1",
        STREAM="1",
        IDLE_GAP="0",
        GAP="0",
        NO_FETCH="1",
        PROGRESS_SNAPSHOT="snap",
        COUNTDOWN_SLEEP="0",
        SIGNATURE_CMD="echo sig",
    )
    return subprocess.run([str(CCLOOP), role], capture_output=True, text=True, env=env, timeout=20)


def test_idle_sentinel_only_in_final_response_line() -> None:
    """#696: a sentinel in a tool result, or named in prose, is not an idle tick."""
    tool_result = {
        "event": "step_update",
        "step_update": {
            "step_type": "tool_result",
            "state": "DONE",
            "output": "184: `LOOP-REVIEWER-IDLE` on its own and stop\nLOOP-REVIEWER-IDLE",
        },
    }
    cases = [
        (
            "tool result quotes the sentinel, final message does not",
            [tool_result, {"event": "result", "result": {"status": "SUCCESS", "response": "Dispatched implementer for #694."}}],
            False,
        ),
        (
            "final message names the sentinel to say it is not printing it",
            [{"event": "result", "result": {"status": "SUCCESS", "response": "アイドル標識（`LOOP-REVIEWER-IDLE`）は出力せず本ティックを終了します"}}],
            False,
        ),
        (
            "final message ends with the sentinel on its own line",
            [tool_result, {"event": "result", "result": {"status": "SUCCESS", "response": "Nothing to do.\n\nLOOP-REVIEWER-IDLE\n"}}],
            True,
        ),
    ]
    for label, events, want_idle in cases:
        with tempfile.TemporaryDirectory() as tmpdir:
            res = _run_stream_tick(pathlib.Path(tmpdir), "reviewer", _stream(events))
            check(f"#696 {label}: counted idle", "reviewer idle (1 in a row)" in res.stdout, want_idle)
            check(f"#696 {label}: counted busy", "busy but produced no activity" in res.stdout, not want_idle)


def test_countdown_wakes_early_on_wake_file() -> None:
    """#698: a 600 s backoff ends within 2 s of $LOGDIR/wake appearing."""
    import time

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = pathlib.Path(tmpdir)
        stub = tmp_path / "stub_idle.sh"
        stub.write_text("#!/usr/bin/env bash\necho LOOP-NOTHING-TO-MERGE\n", encoding="utf-8")
        stub.chmod(0o755)
        log_dir = tmp_path / "logs"
        env = dict(os.environ)
        env.update(
            CCROLE=str(stub),
            LOGDIR=str(log_dir),
            MAX="1",
            STREAM="0",
            IDLE_GAP="600",
            GAP="0",
            NO_FETCH="1",
            PROGRESS_SNAPSHOT="snap",
            SIGNATURE_CMD="echo sig",
        )
        proc = subprocess.Popen(
            [str(CCLOOP), "coder"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, start_new_session=True
        )
        out, elapsed = "", float("inf")
        try:
            # the stub returns at once, so the loop is inside its 600 s countdown well before this
            time.sleep(2)
            woke_at = time.monotonic()
            (log_dir / "wake").write_text("1\n")
            out, _ = proc.communicate(timeout=10)
            elapsed = time.monotonic() - woke_at
        except subprocess.TimeoutExpired:
            pass
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, 9)  # the countdown's sleep too, not just bash (#607)
                proc.wait()
        check("#698 countdown returns within 2 s of the wake file", elapsed < 2.0, True)
        check("#698 countdown reports the wake", "woken by ccwake" in out, True)
        check("#698 wake file is consumed", (log_dir / "wake").exists(), False)
        sig_file = log_dir / "signature"
        check("#698 tick recorded its signature", sig_file.read_text().strip() if sig_file.exists() else None, "sig")


def _fixture(prs=(), triage=(), ready=(), live=(), closed=(), main="m1") -> dict:
    return {
        "data": {
            "repository": {
                "pullRequests": {"nodes": [{"number": n, "headRefOid": sha, "comments": {"nodes": [{"id": c}]}} for n, sha, c in prs]},
                "triage": {"nodes": [{"number": n} for n in triage]},
                "ready": {"nodes": [{"number": n} for n in ready]},
                "live": {"nodes": [{"number": n, "comments": {"nodes": [{"id": c}]}} for n, c in live]},
                "closed": {"nodes": [{"number": n, "labels": {"nodes": [{"name": l} for l in labels]}} for n, labels in closed]},
                "main": {"target": {"oid": main}},
            }
        }
    }


def _wake_once(tmp_path: pathlib.Path, fixture: dict, dirs: dict[str, pathlib.Path]) -> str:
    fx = tmp_path / "fixture.json"
    fx.write_text(json.dumps(fixture), encoding="utf-8")
    env = dict(os.environ)
    env.update(
        WAKE_ONCE="1",
        WAKE_ROLES=" ".join(f"{r}:{d}" for r, d in dirs.items()),
        SIGNATURE_ALL_CMD=f"python3 {CCWAKE_SIG} --all --fixture {fx}",
    )
    return subprocess.run(["bash", str(CCWAKE)], capture_output=True, text=True, env=env, timeout=20).stdout


def _record(tmp_path: pathlib.Path, fixture: dict, role: str, logdir: pathlib.Path) -> None:
    fx = tmp_path / "fixture-record.json"
    fx.write_text(json.dumps(fixture), encoding="utf-8")
    sig = subprocess.run(
        ["python3", str(CCWAKE_SIG), "--role", role, "--fixture", str(fx)], capture_output=True, text=True, check=True
    ).stdout.strip()
    logdir.mkdir(parents=True, exist_ok=True)
    (logdir / "signature").write_text(sig + "\n")


def test_signature_self_write_immunity() -> None:
    """#698: state the role's own tick produced is recorded at tick end, so it does not wake the role."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = pathlib.Path(tmpdir)
        rev = tmp_path / "rev"
        # the reviewer's tick moved #700 triage -> ready and commented on PR 5; ccloop recorded that at the end
        after_own_tick = _fixture(prs=[(5, "abc", "c2")], ready=[700])
        _record(tmp_path, after_own_tick, "reviewer", rev)
        out = _wake_once(tmp_path, after_own_tick, {"reviewer": rev})
        check("#698 own writes do not wake the reviewer", (rev / "wake").exists(), False)
        check("#698 no wake line printed", "woke" in out, False)
        # no record yet (ccloop has not finished a tick): never wake
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        _wake_once(tmp_path, _fixture(ready=[1]), {"reviewer": fresh})
        check("#698 a role with no recorded signature is not woken", (fresh / "wake").exists(), False)


def test_coder_refusal_wakes_reviewer() -> None:
    """#698: stage:pr -> stage:triage plus a refusal comment on the PR wakes the reviewer, not the tester."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = pathlib.Path(tmpdir)
        rev, cod, tst = tmp_path / "rev", tmp_path / "cod", tmp_path / "tst"
        before = _fixture(prs=[(697, "abc", "c1")])
        for role, d in (("reviewer", rev), ("coder", cod), ("tester", tst)):
            _record(tmp_path, before, role, d)
        refused = _fixture(prs=[(697, "abc", "c9")], triage=[694])
        out = _wake_once(tmp_path, refused, {"reviewer": rev, "coder": cod, "tester": tst})
        check("#698 coder refusal wakes the reviewer", (rev / "wake").exists(), True)
        check("#698 coder refusal does not wake the coder", (cod / "wake").exists(), False)
        check("#698 coder refusal does not wake the tester", (tst / "wake").exists(), False)
        check("#698 wake is logged", "woke reviewer" in out, True)
        # a push to main wakes the tester
        pushed = _fixture(prs=[(697, "abc", "c9")], triage=[694], main="m2")
        _wake_once(tmp_path, pushed, {"tester": tst})
        check("#698 a push to main wakes the tester", (tst / "wake").exists(), True)
        # a failed poll never wakes anyone
        env = dict(os.environ)
        env.update(WAKE_ONCE="1", WAKE_ROLES=f"coder:{cod}", SIGNATURE_ALL_CMD="false")
        subprocess.run(["bash", str(CCWAKE)], capture_output=True, text=True, env=env, timeout=20)
        check("#698 a failed poll does not wake", (cod / "wake").exists(), False)


def test_mid_tick_work_arrival_wakes_role() -> None:
    """#707: when outside work arrives mid-tick during an idle run, the signature is not absorbed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = pathlib.Path(tmpdir)
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        fx_before_file = tmp_path / "fx_before.json"
        fx_after_file = tmp_path / "fx_after.json"
        fx_current_file = tmp_path / "fx_current.json"

        fx_before = _fixture(prs=[])
        fx_after = _fixture(prs=[(706, "sha706", "c1")])

        fx_before_file.write_text(json.dumps(fx_before), encoding="utf-8")
        fx_after_file.write_text(json.dumps(fx_after), encoding="utf-8")
        shutil.copy(fx_before_file, fx_current_file)

        before_sig = subprocess.run(
            ["python3", str(CCWAKE_SIG), "--role", "coder", "--fixture", str(fx_before_file)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        after_sig = subprocess.run(
            ["python3", str(CCWAKE_SIG), "--role", "coder", "--fixture", str(fx_after_file)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        check("#707 test fixture signatures differ", before_sig != after_sig, True)

        # Stub ccrole: simulates PR #706 arriving mid-tick while coder runs
        stub = tmp_path / "stub_mid_tick.sh"
        stub.write_text(
            f"""#!/usr/bin/env bash
cp "{fx_after_file}" "{fx_current_file}"
echo "LOOP-NOTHING-TO-MERGE"
exit 0
""",
            encoding="utf-8",
        )
        stub.chmod(0o755)

        sig_cmd = f"python3 {CCWAKE_SIG} --role coder --fixture {fx_current_file}"

        env = dict(os.environ)
        env.update(
            CCROLE=str(stub),
            LOGDIR=str(log_dir),
            MAX="1",
            STREAM="0",
            IDLE_GAP="0",
            GAP="0",
            NO_FETCH="1",
            PROGRESS_SNAPSHOT="snap",
            COUNTDOWN_SLEEP="0",
            SIGNATURE_CMD=sig_cmd,
        )

        res = subprocess.run(
            [str(CCLOOP), "coder"],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )
        check("#707 ccloop exits 0 on idle tick", res.returncode, 0)
        check("#707 ccloop woke early when work arrived mid-tick", "woken by ccwake" in res.stdout, True)

        sig_file = log_dir / "signature"
        recorded_sig = sig_file.read_text().strip() if sig_file.exists() else None
        check("#707 signature was not absorbed into post-tick signature", recorded_sig != after_sig, True)
        check("#707 signature remains pre-tick signature", recorded_sig, before_sig)

        # ccwake sees the diff against pre_sig and wakes coder
        wake_out = _wake_once(tmp_path, fx_after, {"coder": log_dir})
        check("#707 ccwake wakes the coder", (log_dir / "wake").exists(), True)
        check("#707 ccwake logs the wake", "woke coder" in wake_out, True)

        # Active tick: records post_sig as normal (self-write immunity)
        log_dir_active = tmp_path / "logs_active"
        log_dir_active.mkdir(parents=True, exist_ok=True)
        shutil.copy(fx_before_file, fx_current_file)

        stub_active = tmp_path / "stub_active.sh"
        stub_active.write_text(
            f"""#!/usr/bin/env bash
cp "{fx_after_file}" "{fx_current_file}"
echo "Merged PR 706"
exit 0
""",
            encoding="utf-8",
        )
        stub_active.chmod(0o755)

        env["CCROLE"] = str(stub_active)
        env["LOGDIR"] = str(log_dir_active)
        res_active = subprocess.run(
            [str(CCLOOP), "coder"],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )
        check("#707 active ccloop exits 0", res_active.returncode, 0)
        sig_active = (log_dir_active / "signature").read_text().strip() if (log_dir_active / "signature").exists() else None
        check("#707 active tick records post_sig for self-write immunity", sig_active, after_sig)
        _wake_once(tmp_path, fx_after, {"coder": log_dir_active})
        check("#707 active tick does not wake role", (log_dir_active / "wake").exists(), False)


def main() -> None:
    test_syntax_and_executable()
    test_invalid_role()
    test_ccloop_configuration_and_role_defaults()
    test_execution_with_stub_ccrole()
    test_heartbeat_contract_definitions()
    test_heartbeat_marker_parsing()
    test_heartbeat_stall_thresholds()
    test_idle_sentinel_triage_drain_constraint()
    test_tester_prompt_and_idle_sentinel_stage_live_constraint()
    test_print_timeout_and_stranded_work_rescue()
    test_timeout_rescue_on_elapsed_time_without_stderr_message()
    test_timeout_rescue_scoped_to_reviewer_role()
    test_normal_exit_without_timeout()
    test_transient_startup_failure_fast_retry()
    test_persistent_startup_failure_handling()
    test_success_clears_status_and_resets_start_fails()
    test_idle_sentinel_only_in_final_response_line()
    test_countdown_wakes_early_on_wake_file()
    test_signature_self_write_immunity()
    test_coder_refusal_wakes_reviewer()
    test_mid_tick_work_arrival_wakes_role()


    if FAILURES:
        print(f"FAILED ({len(FAILURES)} failures):", file=sys.stderr)
        for f in FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    print("all ccloop and heartbeat cases hold")


if __name__ == "__main__":
    main()
