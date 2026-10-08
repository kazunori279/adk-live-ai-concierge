import datetime
import json
import os
from pathlib import Path
import re
import statistics
from typing import Optional

PROJECT_DIR = Path(__file__).resolve().parent.parent
METRICS_FILE = PROJECT_DIR / "tst_metrics.jsonl"
if not METRICS_FILE.exists():
    METRICS_FILE = Path("/tmp/tst-metrics.jsonl")
RESULTS_FILE = PROJECT_DIR / "current_test_results.md"
TRAJECTORIES_FILE = Path("/tmp/tst-trajectories.jsonl")
TOTAL_TRAJECTORIES = 10
try:
    from tools.tst_trajectories import SCENARIOS as _TRAJ_SCENARIOS
    TOTAL_TRAJECTORIES = len(_TRAJ_SCENARIOS)
except Exception:
    try:
        from tst_trajectories import SCENARIOS as _TRAJ_SCENARIOS
        TOTAL_TRAJECTORIES = len(_TRAJ_SCENARIOS)
    except Exception:
        TOTAL_TRAJECTORIES = 10

CAPABILITIES_FILE = Path(__file__).resolve().parent / "tst_capabilities.yml"
if not CAPABILITIES_FILE.exists():
    CAPABILITIES_FILE = PROJECT_DIR / "tools" / "tst_capabilities.yml"


def load_capabilities(path=None):
    """Load tracked capabilities list from YAML file (fallback gracefully if missing)."""
    p = Path(path) if path is not None else CAPABILITIES_FILE
    if not p.exists():
        return []
    try:
        import yaml
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def compute_coverage_metrics(capabilities=None):
    """Compute covered, tracked (or total), gaps, excluded dynamically from capabilities."""
    if capabilities is None:
        capabilities = load_capabilities()
    covered = sum(1 for c in capabilities if isinstance(c, dict) and str(c.get("status", "")).lower() == "covered")
    excluded = sum(1 for c in capabilities if isinstance(c, dict) and str(c.get("status", "")).lower() == "excluded")
    gaps = sum(1 for c in capabilities if isinstance(c, dict) and str(c.get("status", "")).lower() == "gap")
    tracked = len(capabilities)
    return {
        "covered": covered,
        "tracked": tracked,
        "total": tracked,
        "gaps": gaps,
        "excluded": excluded,
    }



def is_traj(item):
    sc = item.get("scenario", "")
    return sc.startswith("traj-") or sc.startswith("trajectory-") or "turns" in item


def render_results(current_run_id=None, traj_ran: Optional[bool] = None):
    if not os.path.exists(METRICS_FILE):
        content = "# Test results\n\nNo metrics recorded yet.\n"
        with open(RESULTS_FILE, "w", encoding="utf-8") as f:
            f.write(content)
        return

    with open(METRICS_FILE, "r", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]

    if not rows:
        content = "# Test results\n\nNo metrics recorded yet.\n"
        with open(RESULTS_FILE, "w", encoding="utf-8") as f:
            f.write(content)
        return

    runs = []
    cur_run = []
    last_t = None
    for r in rows:
        t = datetime.datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
        # group by run_id if present, else by 120s time proximity
        if r.get("run_id"):
            if cur_run and cur_run[-1].get("run_id") != r.get("run_id"):
                runs.append(cur_run)
                cur_run = []
        else:
            if last_t is not None and (t - last_t).total_seconds() > 120:
                if cur_run:
                    runs.append(cur_run)
                cur_run = []
        cur_run.append(r)
        last_t = t
    if cur_run:
        runs.append(cur_run)

    # Separate single-turn runs and trajectory runs
    single_runs = []
    traj_runs = []
    for r in runs:
        singles = [x for x in r if not is_traj(x)]
        trajs = [x for x in r if is_traj(x)]
        if singles:
            single_runs.append(singles)
        if trajs:
            traj_runs.append(trajs)

    # Fallback to load trajectories from /tmp/tst-trajectories.jsonl if not in metrics
    if not traj_runs and TRAJECTORIES_FILE.exists():
        try:
            with open(TRAJECTORIES_FILE, "r", encoding="utf-8") as f:
                t_rows = [json.loads(line) for line in f if line.strip()]
            if t_rows:
                t_runs_map = {}
                for tr in t_rows:
                    t_runs_map.setdefault(tr.get("run_id", "run_traj"), []).append(tr)
                traj_runs = list(t_runs_map.values())
        except Exception:
            pass

    last_run = single_runs[-1] if single_runs else runs[-1]
    prev_runs = single_runs[-11:-1] if len(single_runs) > 1 else (runs[-11:-1] if len(runs) > 1 else [])
    last_row = last_run[0]
    sha = last_row.get("sha", "unknown")
    dirty = last_row.get("dirty", False)
    dirty_str = "dirty" if dirty else "clean"
    run_id = last_row.get("run_id") or current_run_id or "run_latest"

    dt_utc = datetime.datetime.fromisoformat(last_row["ts"].replace("Z", "+00:00"))
    dt_jst = dt_utc.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
    t_jst_str = dt_jst.strftime("%Y-%m-%d %H:%M JST")

    passes = sum(1 for x in last_run if x.get("pass"))
    total = len(last_run)
    total_elapsed_ms = sum(x.get("elapsed_ms", 0) for x in last_run)
    total_elapsed_s = total_elapsed_ms / 1000.0

    mode_str = "idle tick: regression + baseline" if total < 19 else "full suite"

    cov = compute_coverage_metrics(load_capabilities(CAPABILITIES_FILE))
    covered = cov["covered"]
    tracked = cov["tracked"]
    gaps = cov["gaps"]
    excluded = cov["excluded"]

    lines = []
    lines.append(f"# Test results — {t_jst_str} · {sha} ({dirty_str}) · {run_id}\n")
    lines.append(f"{passes}/{total} pass ({mode_str}) · coverage {covered}/{tracked} ({gaps} gaps, {excluded} excluded) · run {total_elapsed_s:.1f} s\n")
    lines.append("## Last run\n")
    lines.append("| scenario | pass | first token | complete | elapsed | tools | vs 10-run median |")
    lines.append("|---|---|---|---|---|---|---|")

    for item in last_run:
        sc = item["scenario"]
        p = "ok" if item.get("pass") else "FAIL"

        ft_val = item.get("first_token_ms")
        ft = f"{ft_val/1000:.1f} s" if ft_val is not None else "—"

        comp_val = item.get("complete_ms")
        comp = f"{comp_val/1000:.1f} s" if comp_val is not None else "—"

        el_ms = item.get("elapsed_ms", 0)
        el = f"{el_ms/1000:.1f} s" if el_ms >= 1000 else f"{el_ms} ms"

        tools_list = item.get("tools", [])
        tools = ", ".join(tools_list) if tools_list else "—"

        curr_metric = comp_val or el_ms or 0
        prev_vals = []
        for pr in prev_runs:
            for pr_item in pr:
                if pr_item["scenario"] == sc:
                    v = pr_item.get("complete_ms") or pr_item.get("elapsed_ms")
                    if v is not None:
                        prev_vals.append(v)
        if prev_vals:
            med = statistics.median(prev_vals)
            ratio = curr_metric / med if med > 0 else 1.0
            diff = abs(curr_metric - med)
            if (ratio >= 2.0 or ratio <= 0.5) and diff >= 500:
                vs_str = f"**{ratio:.1f}×**"
            else:
                vs_str = f"{ratio:.1f}×"
        else:
            vs_str = "—"
        lines.append(f"| {sc} | {p} | {ft} | {comp} | {el} | {tools} | {vs_str} |")

    # Multi-turn Trajectory Section
    if traj_runs:
        sha_traj_runs = [r for r in traj_runs if r[0].get("sha") == sha]
        if sha_traj_runs:
            last_traj_run = sha_traj_runs[-1]
            if traj_ran is False:
                is_stale = True
            elif traj_ran is True:
                is_stale = False
            elif current_run_id is not None:
                traj_run_id = last_traj_run[0].get("run_id")
                is_stale = (traj_run_id != current_run_id)
            else:
                last_run_in_file = runs[-1] if runs else []
                latest_has_traj = any(is_traj(x) for x in last_run_in_file)
                is_stale = not latest_has_traj

            if is_stale:
                traj_ts = last_traj_run[0].get("ts", "unknown")
                traj_sha = last_traj_run[0].get("sha", sha)
                traj_sha_note = f"(trajectories skipped this run; showing cached results from {traj_ts} / {traj_sha})\n"
            else:
                traj_sha_note = ""
        else:
            last_traj_run = traj_runs[-1]
            traj_prev_sha = last_traj_run[0].get("sha", "unknown")
            traj_sha_note = f"No trajectory results for {sha} (showing previous run from {traj_prev_sha})\n"

        t_passes = sum(1 for x in last_traj_run if x.get("pass"))
        t_total = len(last_traj_run)
        t_elapsed_s = sum(x.get("elapsed_ms", 0) for x in last_traj_run) / 1000.0
        lines.append("\n## Multi-Turn Trajectories\n")
        if traj_sha_note:
            lines.append(traj_sha_note)
        if t_total < TOTAL_TRAJECTORIES:
            lines.append(f"{t_total}/{TOTAL_TRAJECTORIES} scenarios run, {t_passes} pass (incomplete) · run {t_elapsed_s:.1f} s\n")
        else:
            lines.append(f"{t_total}/{TOTAL_TRAJECTORIES} scenarios run, {t_passes} pass · run {t_elapsed_s:.1f} s\n")
        lines.append("| scenario | pass | turns | context | first token | complete | elapsed | tools | screenshot |")
        lines.append("|---|---|:---:|:---:|---|---|---|---|---|")
        for item in last_traj_run:
            sc = item.get("scenario", "")
            p = "ok" if item.get("pass") else "FAIL"
            t_val = item.get("turns")
            if isinstance(t_val, list):
                turns_cnt = len(t_val)
            elif isinstance(t_val, int):
                turns_cnt = t_val
            elif "total_turns" in item:
                turns_cnt = item["total_turns"]
            else:
                turns_cnt = 3
            ctx = "ok" if item.get("context_retained") is True else ("skipped" if item.get("context_retained") is None else "lost")

            ft_val = item.get("first_token_ms")
            ft = f"{ft_val/1000:.1f} s" if ft_val is not None else "—"

            comp_val = item.get("complete_ms")
            comp = f"{comp_val/1000:.1f} s" if comp_val is not None else "—"

            el_ms = item.get("elapsed_ms", 0)
            el = f"{el_ms/1000:.1f} s" if el_ms >= 1000 else f"{el_ms} ms"

            tools_list = item.get("tools", [])
            tools = ", ".join(tools_list) if tools_list else "—"

            shot = os.path.basename(item.get("screenshot", "")) or "—"
            lines.append(f"| {sc} | {p} | {turns_cnt} | {ctx} | {ft} | {comp} | {el} | {tools} | {shot} |")
    else:
        lines.append("\n## Multi-Turn Trajectories\n")
        lines.append(f"No trajectory results recorded yet (none for {sha}).\n")

    lines.append("\n## Pass rate, last 20 runs\n```text")
    for r in (single_runs[-20:] if single_runs else runs[-20:]):
        r_sha = r[0].get("sha", "unknown")
        r_passes = sum(1 for x in r if x.get("pass"))
        r_total = len(r)
        frac = r_passes / r_total if r_total else 0
        filled = int(round(frac * 20))
        bar = "█" * filled + "·" * (20 - filled)
        r_dt = datetime.datetime.fromisoformat(r[0]["ts"].replace("Z", "+00:00")).astimezone(datetime.timezone(datetime.timedelta(hours=9)))
        r_t_jst = r_dt.strftime("%Y-%m-%d %H:%M")
        lines.append(f"{r_sha:<8} {bar} {r_passes:>2}/{r_total:<2} ({r_t_jst})")
    lines.append("```\n")

    medians = []
    for r in (single_runs[-20:] if single_runs else runs[-20:]):
        c_times = [x["complete_ms"] for x in r if x.get("complete_ms") is not None and not is_traj(x)]
        if c_times:
            medians.append(statistics.median(c_times))
        else:
            medians.append(None)
    valid = [m for m in medians if m is not None]
    lines.append("## Median complete_ms, last 20 runs\n```text")
    if valid:
        min_v, max_v = min(valid), max(valid)
        blocks = [" ", " ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
        spark = ""
        for m in medians:
            if m is None:
                spark += "·"
            elif max_v == min_v:
                spark += blocks[0]
            else:
                idx = int(round((m - min_v) / (max_v - min_v) * (len(blocks) - 1)))
                spark += blocks[idx]
        lines.append(f"{spark}   {int(min_v)} → {int(max_v)} ms")
    else:
        lines.append("No complete_ms metrics recorded in window.")
    lines.append("```\n")

    if traj_runs:
        lines.append("## Trajectory pass rate, last 20 runs\n```text")
        for r in traj_runs[-20:]:
            r_sha = r[0].get("sha", "unknown")
            r_passes = sum(1 for x in r if x.get("pass"))
            r_total = len(r)
            frac = r_passes / TOTAL_TRAJECTORIES if TOTAL_TRAJECTORIES else 0
            filled = int(round(frac * 20))
            bar = "█" * filled + "·" * (20 - filled)
            r_dt = datetime.datetime.fromisoformat(r[0]["ts"].replace("Z", "+00:00")).astimezone(datetime.timezone(datetime.timedelta(hours=9)))
            r_t_jst = r_dt.strftime("%Y-%m-%d %H:%M")
            lines.append(f"{r_sha:<8} {bar} {r_passes:>2}/{r_total:<2} ({r_t_jst})")
        lines.append("```\n")

        traj_medians = []
        for r in traj_runs[-20:]:
            c_times = [x["complete_ms"] for x in r if x.get("complete_ms") is not None]
            if c_times:
                traj_medians.append(statistics.median(c_times))
            else:
                traj_medians.append(None)
        traj_valid = [m for m in traj_medians if m is not None]
        lines.append("## Trajectory median complete_ms, last 20 runs\n```text")
        if traj_valid:
            min_v, max_v = min(traj_valid), max(traj_valid)
            blocks = [" ", " ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
            spark = ""
            for m in traj_medians:
                if m is None:
                    spark += "·"
                elif max_v == min_v:
                    spark += blocks[0]
                else:
                    idx = int(round((m - min_v) / (max_v - min_v) * (len(blocks) - 1)))
                    spark += blocks[idx]
            lines.append(f"{spark}   {int(min_v)} → {int(max_v)} ms")
        else:
            lines.append("No complete_ms metrics recorded in window.")
        lines.append("```\n")

    # Visual friction log, last run (#473, #524, #573)
    lines.append("## Visual friction log, last run")
    friction_found = False
    judge_errors = []
    current_symptoms = set()

    for item in last_run:
        err = item.get("friction_error")
        if err:
            judge_errors.append((item.get("scenario"), str(err)))
        f = item.get("friction")
        if f and f != "none" and not str(f).startswith("friction check skipped"):
            if "RESOURCE_EXHAUSTED" in str(f) or "friction error:" in str(f) or "429" in str(f):
                if not err:
                    judge_errors.append((item.get("scenario"), str(f)))
                continue
            shot_name = os.path.basename(item.get("screenshot") or "") or "—"
            lines.append(f"- **{item.get('scenario')}** (`{shot_name}`): {f}")
            current_symptoms.add((item.get("scenario"), str(f)))
            friction_found = True

    # Cross-run symptom tracking against prev_runs (#573)
    absent_symptoms = {}
    if prev_runs:
        for p_run in reversed(prev_runs):
            if not p_run:
                continue
            p_run_id = p_run[0].get("run_id") or "prev"
            for item in p_run:
                perr = item.get("friction_error")
                pf = item.get("friction")
                if perr:
                    continue
                if pf and pf != "none" and not str(pf).startswith("friction check skipped"):
                    if "RESOURCE_EXHAUSTED" in str(pf) or "friction error:" in str(pf) or "429" in str(pf):
                        continue
                    sc = item.get("scenario") or "unknown"
                    key = (sc, str(pf))
                    if key not in current_symptoms and key not in absent_symptoms:
                        shot_name = os.path.basename(item.get("screenshot") or "") or "—"
                        item_run_id = item.get("run_id") or p_run_id
                        absent_symptoms[key] = (shot_name, item_run_id)

    for (sc, pf), (shot_name, item_run_id) in sorted(absent_symptoms.items()):
        lines.append(f"- **{sc}** (`{shot_name}`): {pf} (absent this run, last seen: {item_run_id})")
        friction_found = True

    if not friction_found:
        lines.append("No visual friction detected.")
    lines.append("")

    if judge_errors:
        err_scenarios = ", ".join(f"`{sc}`" for sc, _ in judge_errors if sc)
        lines.append(f"> Tooling health: {len(judge_errors)} judge evaluation(s) encountered error ({err_scenarios}).")
    lines.append("> Metrics store: `tst_metrics.jsonl` at project root (gitignored).")
    lines.append("> Dashboard: `current_test_results.md` at project root (gitignored).")
    lines.append("> Trajectory details: `/tmp/tst-trajectories.jsonl` & `/tmp/tst-trajectories.md`.\n")

    content = "\n".join(lines)
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Wrote results to {RESULTS_FILE}")


def validate_friction_log(content_or_path: str | Path) -> tuple[bool, str]:
    """Validate visual friction log format in test results markdown (#524).

    Returns (True, "") if valid, or (False, error_description) if malformed.
    """
    if isinstance(content_or_path, Path):
        if not content_or_path.is_file():
            return True, ""
        content = content_or_path.read_text(encoding="utf-8")
    elif isinstance(content_or_path, str) and "\n" not in content_or_path and content_or_path.strip() and Path(content_or_path).is_file():
        content = Path(content_or_path).read_text(encoding="utf-8")
    else:
        content = str(content_or_path)

    # Extract ## Visual friction log, last run section if present
    header = "## Visual friction log, last run"
    if header in content:
        start = content.index(header) + len(header)
        next_h = re.search(r"\n#{1,2}\s", content[start:])
        sec_text = content[start:start + next_h.start()] if next_h else content[start:]
    else:
        sec_text = content

    sec_stripped = sec_text.strip()
    symptom_bullets = [
        line.strip() for line in sec_text.splitlines() if line.strip().startswith("- **")
    ]

    # If empty or explicitly clean with no symptom bullets, it is valid
    if not sec_stripped or ("No visual friction detected." in sec_text and not symptom_bullets):
        return True, ""

    # If symptom bullets exist, summary count line is required
    count_re = re.compile(
        r"(?:friction\s+)?(\d+)\s*(?:raw\s+)?lines\s*->\s*(\d+)\s*(?:distinct\s+)?symptoms:\s*(.*)",
        re.IGNORECASE
    )
    if not count_re.search(sec_text):
        return False, "missing count line ('friction N lines -> M symptoms: ...')"

    # Validate each symptom entry line starting with - **
    for line in symptom_bullets:
        if "RESOURCE_EXHAUSTED" in line or "friction error:" in line:
            return False, f"entry contains judge error rather than user symptom: {line}"

        if "->" not in line:
            return False, f"entry missing valid disposition: {line}"

        disp_part = line.rsplit("->", 1)[-1].strip()
        disp_clean = disp_part.lstrip("` \t")

        m_filed = re.match(r"^filed\s*#\d+", disp_clean, re.IGNORECASE)
        m_dup = re.match(r"^dup\s*#\d+", disp_clean, re.IGNORECASE)
        m_undecided = re.match(r"^undecided\b", disp_clean, re.IGNORECASE)
        m_rig = re.match(r"^rig\b", disp_clean, re.IGNORECASE)

        if not (m_filed or m_dup or m_undecided or m_rig):
            return False, f"entry missing valid disposition: {line}"

        if m_rig:
            after_rig = disp_clean[3:].strip()
            if after_rig.startswith("`"):
                after_rig = after_rig.lstrip("`").strip()
            reason_content = re.sub(r"[\s\(\):`\.,;]", "", after_rig)
            if not reason_content:
                return False, f"entry missing valid disposition: {line}"

    return True, ""


if __name__ == "__main__":
    render_results()
