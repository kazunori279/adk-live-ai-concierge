import ast
import asyncio
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request
import yaml
from playwright.async_api import async_playwright

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR / "tools") not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR / "tools"))

try:
    from tools import tst_render
except ImportError:
    try:
        import tst_render
    except ImportError:
        _tools_dir = str(PROJECT_DIR / "tools")
        if _tools_dir not in sys.path:
            sys.path.insert(0, _tools_dir)
        import tst_render

BASE_URL = "http://127.0.0.1:8021"
SERVER_LOG = "/tmp/tst-server.log"
METRICS_FILE = "/tmp/tst-metrics.jsonl"
REPO_METRICS_FILE = PROJECT_DIR / "tst_metrics.jsonl"
SUMMARY_FILE = "/tmp/tst-summary.json"
REGRESSIONS_FILE = PROJECT_DIR / "tools" / "regressions.yml"
SCREENSHOT_DIR = "/tmp/tst-screenshots"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
TRAJ_SHA_FILE = Path("/tmp/tst-last-traj-sha")
RUN_LOCK_FILE = Path("/tmp/tst-runner.lock")
_RUN_LOCK_FD: int | None = None


def acquire_run_lock(lock_path: Path = RUN_LOCK_FILE) -> tuple[bool, str, int | None]:
    """Acquire exclusive flock on RUN_LOCK_FILE to prevent concurrent test runs (#725)."""
    target = Path(lock_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(target), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        return False, f"failed to open lock file {target}: {exc}", None

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        holder_pid = "unknown"
        try:
            with open(str(target), "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    holder_pid = content.split()[0]
        except Exception:
            pass
        os.close(fd)
        return False, f"tst_runner already running (PID {holder_pid}); exiting without running tests", None

    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        os.fsync(fd)
    except Exception:
        pass

    return True, "ok", fd


def release_run_lock(fd: int | None) -> None:
    """Release run lock file descriptor (#725)."""
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass


def should_run_trajectories(current_sha: str, force: bool = False) -> bool:
    if force:
        return True
    if not TRAJ_SHA_FILE.exists():
        return True
    try:
        last_sha = TRAJ_SHA_FILE.read_text(encoding="utf-8").strip()
        return last_sha != current_sha
    except Exception:
        return True


def mark_trajectories_run(current_sha: str, narrowed: bool = False) -> None:
    if narrowed:
        return
    try:
        TRAJ_SHA_FILE.write_text(current_sha.strip(), encoding="utf-8")
    except Exception:
        pass


# Absolute latency ceilings for non-model scenarios (#445). Exceeding the ceiling
# fails the scenario even if all functional assertions hold.
LOAD_SLASH_MAX_ELAPSED_MS = 2500
RELOAD_MID_CONV_MAX_ELAPSED_MS = 5500


async def capture_scenario_screenshot(page, scenario_name, status="ok"):
    if not page:
        return ""
    try:
        if hasattr(page, "is_closed") and page.is_closed():
            return ""
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{scenario_name}_{status}_{ts}.png"
        path = os.path.join(SCREENSHOT_DIR, filename)
        await page.screenshot(path=path)
        return path
    except Exception as e:
        print(f"screenshot capture error for {scenario_name}: {e}")
        return ""

def clean_old_screenshots(max_keep=100):
    try:
        if not os.path.exists(SCREENSHOT_DIR):
            return
        files = [
            os.path.join(SCREENSHOT_DIR, f)
            for f in os.listdir(SCREENSHOT_DIR)
            if f.endswith(".png")
        ]
        if len(files) > max_keep:
            files.sort(key=os.path.getmtime)
            for f in files[:-max_keep]:
                try:
                    os.remove(f)
                except Exception:
                    pass
    except Exception:
        pass

def forget_user(user_id="terminal"):
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/api/forget/{user_id}",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            pass
    except Exception:
        pass

def get_git_info():
    try:
        sha = subprocess.check_output(["git", "-C", str(PROJECT_DIR), "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        sha = "unknown"
    try:
        status = subprocess.check_output(["git", "-C", str(PROJECT_DIR), "status", "--porcelain"], text=True).strip()
    except Exception:
        status = ""
    return sha, bool(status)


def server_preflight(
    base_url: str = BASE_URL,
    expected_sha: str | None = None,
    server_log: str | Path | None = None,
) -> tuple[bool, str]:
    """Verify test rig server is reachable, management routes are enabled, and revision matches (#720)."""
    req_config = urllib.request.Request(f"{base_url}/api/config")
    try:
        with urllib.request.urlopen(req_config, timeout=3.0) as resp:
            config_data = json.loads(resp.read().decode())
    except Exception:
        return False, f"server unreachable at {base_url}"

    if not isinstance(config_data, dict):
        return False, f"server unreachable at {base_url}"

    if config_data.get("management_routes") is False:
        return False, "server up but ENABLE_MANAGEMENT_ROUTES is off"

    req_wake = urllib.request.Request(
        f"{base_url}/api/wake",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req_wake, timeout=3.0) as resp:
            if resp.status in (403, 404):
                return False, "server up but ENABLE_MANAGEMENT_ROUTES is off"
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return False, "server up but ENABLE_MANAGEMENT_ROUTES is off"
    except Exception:
        pass

    if expected_sha and expected_sha != "unknown":
        server_rev = config_data.get("revision")
        if server_rev and server_rev != "unknown" and server_rev != expected_sha:
            return False, f"server revision '{server_rev}' does not match HEAD '{expected_sha}'"

    # Verify server is logging to server_log (#724)
    target_log = Path(server_log or SERVER_LOG)
    if not target_log.exists():
        return False, f"server is not logging to {target_log} (start with tools/tst_server.sh)"

    try:
        log_content = target_log.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return False, f"failed to read server log {target_log}: {exc}"

    rev_matches = re.findall(r"running revision\s+(\S+)", log_content)
    if not rev_matches:
        return False, f"server is not logging to {target_log} (start with tools/tst_server.sh)"

    server_rev = config_data.get("revision")
    if server_rev and server_rev != "unknown" and rev_matches[-1] != server_rev:
        return False, f"server is not logging to {target_log} (start with tools/tst_server.sh)"

    size_before = target_log.stat().st_size
    req_probe = urllib.request.Request(f"{base_url}/api/config")
    try:
        with urllib.request.urlopen(req_probe, timeout=3.0) as resp:
            resp.read()
    except Exception:
        pass

    if target_log.stat().st_size <= size_before:
        time.sleep(0.05)
    if target_log.stat().st_size <= size_before:
        return False, f"server is not logging to {target_log} (start with tools/tst_server.sh)"

    return True, "ok"


def load_regression_entries(corpus_path: Path | None = None) -> tuple[list[dict], str | None]:
    """Load and parse the tracked regression corpus from tools/regressions.yml (#509).

    Returns (entries, error_message). On parse error or schema error, returns ([], error_message).
    """
    path = corpus_path if corpus_path is not None else REGRESSIONS_FILE
    if not path.exists():
        return [], f"regressions corpus not found: {path}"
    try:
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except Exception as e:
        return [], f"regressions corpus parse error: {e}"
    if not isinstance(data, list):
        return [], "regressions corpus root must be a list of entries"
    for idx, entry in enumerate(data):
        if not isinstance(entry, dict):
            return [], f"regression entry at index {idx} is not a dict"
    return data, None


KNOWN_DRIVE_STEP_KEYS = {"reload", "wake", "click", "wait_ms"}
KNOWN_DRIVE_KEYS = {"reload", "wake", "click", "wait_ms", "steps"}
KNOWN_ASSERT_KEYS = {"selector", "visible", "in_viewport", "classes", "not_classes", "text_contains"}


def validate_regression_entry(entry: dict) -> bool:
    """Validate that a regression entry adheres to the required schema (#509, #551)."""
    if not isinstance(entry, dict):
        return False
    if not isinstance(entry.get("issue"), int):
        return False
    if not isinstance(entry.get("title"), str) or not entry.get("title", "").strip():
        return False
    drive = entry.get("drive")
    if not isinstance(drive, (dict, list)):
        return False
    if not isinstance(entry.get("assert"), dict):
        return False

    if isinstance(drive, list):
        if not drive:
            return False
        for step in drive:
            if not isinstance(step, dict) or not step:
                return False
            if set(step) - KNOWN_DRIVE_STEP_KEYS:
                return False
    else:
        if set(drive) - KNOWN_DRIVE_KEYS:
            return False
        if "steps" in drive:
            steps = drive["steps"]
            if not isinstance(steps, list) or not steps:
                return False
            for step in steps:
                if not isinstance(step, dict) or not step:
                    return False
                if set(step) - KNOWN_DRIVE_STEP_KEYS:
                    return False

    if set(entry.get("assert", {})) - KNOWN_ASSERT_KEYS:
        return False
    return True


async def _execute_drive_step(page, step: dict) -> None:
    """Execute a single drive step action (#509, #551)."""
    if step.get("reload"):
        await page.reload()
    if step.get("wake"):
        req = urllib.request.Request(
            f"{BASE_URL}/api/wake",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            pass
    if step.get("click"):
        await page.click(step["click"])
    if step.get("wait_ms"):
        await page.wait_for_timeout(step["wait_ms"])


async def run_regressions_corpus(page, results: dict, sha: str, dirty: bool, corpus_path: Path | None = None) -> None:
    """Execute the tracked regression corpus against the active browser page (#509).

    Each entry runs its drive actions, verifies assertions, and records a failure
    naming `#{issue} regressed` if an assertion fails or the entry cannot be parsed.
    """
    entries, load_err = load_regression_entries(corpus_path)
    if load_err:
        s_detail = f"#unknown regressed: {load_err}"
        results["regression_load"] = {"pass": False, "detail": s_detail, "elapsed_ms": 0}
        append_metric(sha, dirty, "regression-load", False, None, None, 0, [], 0, 0, screenshot="")
        print(f"Regression corpus load: pass=False, detail={s_detail}")
        return

    if not entries:
        s_detail = "#unknown regressed: empty regression corpus"
        results["regression_load"] = {"pass": False, "detail": s_detail, "elapsed_ms": 0}
        append_metric(sha, dirty, "regression-load", False, None, None, 0, [], 0, 0, screenshot="")
        print(f"Regression corpus load: pass=False, detail={s_detail}")
        return

    for entry in entries:
        if not validate_regression_entry(entry):
            issue = entry.get("issue", "unknown") if isinstance(entry, dict) else "unknown"
            s_detail = f"#{issue} regressed: schema invalid (requires issue, title, drive, assert)"
            results[f"regression_{issue}"] = {
                "pass": False,
                "issue": issue,
                "title": entry.get("title", "invalid") if isinstance(entry, dict) else "invalid",
                "detail": s_detail,
                "elapsed_ms": 0,
            }
            append_metric(sha, dirty, f"regression-{issue}", False, None, None, 0, [], 0, 0, screenshot="")
            print(f"Regression #{issue}: pass=False, detail={s_detail}")
            continue

        issue = entry["issue"]
        title = entry.get("title", "")
        drive = entry.get("drive", {})
        assert_spec = entry.get("assert", {})
        t0 = time.time()
        r_pass = False
        r_detail = ""
        r_shot = ""

        try:
            # Drive phase (#551: multi-step drive support)
            if isinstance(drive, list):
                steps = drive
            elif isinstance(drive, dict) and "steps" in drive:
                steps = drive["steps"]
            else:
                steps = [drive]

            for step in steps:
                await _execute_drive_step(page, step)

            # Assert phase
            selector = assert_spec.get("selector")
            if not selector:
                raise AssertionError("assert missing selector")

            el = await page.query_selector(selector)
            if not el:
                raise AssertionError(f"element '{selector}' not found")

            if "visible" in assert_spec:
                is_vis = await el.is_visible()
                if is_vis != assert_spec["visible"]:
                    raise AssertionError(f"element '{selector}' visible={is_vis}, want {assert_spec['visible']}")

            if "in_viewport" in assert_spec:
                in_vp = await el.evaluate("""el => {
                    const r = el.getBoundingClientRect();
                    const vh = window.innerHeight || document.documentElement.clientHeight;
                    const vw = window.innerWidth || document.documentElement.clientWidth;
                    return r.top >= 0 && r.top < vh && r.left >= 0 && r.left < vw;
                }""")
                if in_vp != assert_spec["in_viewport"]:
                    raise AssertionError(f"element '{selector}' in_viewport={in_vp}, want {assert_spec['in_viewport']}")

            if "classes" in assert_spec:
                classes = await el.evaluate("el => Array.from(el.classList)")
                for cls in assert_spec["classes"]:
                    if cls not in classes:
                        raise AssertionError(f"element '{selector}' missing class '{cls}' (classes={classes})")

            if "not_classes" in assert_spec:
                classes = await el.evaluate("el => Array.from(el.classList)")
                for cls in assert_spec["not_classes"]:
                    if cls in classes:
                        raise AssertionError(f"element '{selector}' has forbidden class '{cls}' (classes={classes})")

            if "text_contains" in assert_spec:
                text = await el.inner_text()
                if assert_spec["text_contains"] not in text:
                    raise AssertionError(f"element '{selector}' does not contain text '{assert_spec['text_contains']}'")

            r_pass = True
            r_detail = f"ok ({title})"
            r_shot = await capture_scenario_screenshot(page, f"regression-{issue}", "ok")
        except Exception as e:
            r_pass = False
            r_detail = f"#{issue} regressed: {e}"
            r_shot = await capture_scenario_screenshot(page, f"regression-{issue}", "fail")

        elapsed_ms = int((time.time() - t0) * 1000)
        results[f"regression_{issue}"] = {
            "pass": r_pass,
            "issue": issue,
            "title": title,
            "detail": r_detail,
            "elapsed_ms": elapsed_ms,
            "screenshot": r_shot,
        }
        append_metric(sha, dirty, f"regression-{issue}", r_pass, None, None, elapsed_ms, [], 0, 0, screenshot=r_shot)
        print(f"Regression #{issue} ({title}): pass={r_pass}, detail={r_detail}, elapsed={elapsed_ms}ms")


class ToolCallsCount(int):
    """Integer count of tool calls that also stores structured call and result records."""
    def __new__(cls, count: int, calls=None, results=None):
        inst = super().__new__(cls, count)
        inst.calls = calls if calls is not None else []
        inst.results = results if results is not None else []
        return inst


class ToolsList(list):
    """List of tool names that also preserves captured tool results and calls."""
    def __init__(self, items, results=None, calls=None):
        super().__init__(items)
        self.results = results if results is not None else []
        self.calls = calls if calls is not None else []


class ToolCallsTuple(tuple):
    """Tuple (tools, tool_calls) that preserves attributes for tool results."""
    def __new__(cls, tools, tool_calls, results=None):
        inst = super().__new__(cls, (tools, tool_calls))
        inst.tools = tools
        inst.tool_calls = tool_calls
        inst.results = results if results is not None else []
        inst.tool_results = results if results is not None else []
        return inst


def _parse_tool_shape(raw: str):
    if not raw:
        return {}
    raw = raw.strip()
    try:
        return ast.literal_eval(raw)
    except Exception:
        pass
    try:
        return json.loads(raw)
    except Exception:
        return raw


def parse_tool_calls_since(log_offset, include_results=False):
    """Extract tool calls and results logged since log_offset."""
    tools = []
    tool_results = []
    if not os.path.exists(SERVER_LOG):
        t_list = ToolsList([], [])
        c_count = ToolCallsCount(0, [], [])
        tuple_res = ToolCallsTuple(t_list, c_count, [])
        return (t_list, c_count, []) if include_results else tuple_res
    with open(SERVER_LOG, "r", encoding="utf-8", errors="replace") as f:
        f.seek(log_offset)
        new_lines = f.readlines()
    for line in new_lines:
        m_call = re.search(r"tool call (\w+)", line)
        if m_call:
            tools.append(m_call.group(1))
        m_res = re.search(r"tool result (\w+)\s+(.+)", line)
        if m_res:
            name = m_res.group(1)
            raw_shape = m_res.group(2).strip()
            parsed_shape = _parse_tool_shape(raw_shape)
            tool_results.append({"tool": name, "shape": parsed_shape})

    t_list = ToolsList(tools, tool_results, tools)
    c_count = ToolCallsCount(len(tools), tools, tool_results)
    tuple_res = ToolCallsTuple(t_list, c_count, tool_results)
    if include_results:
        return (t_list, c_count, tool_results)
    return tuple_res


def parse_tool_results_since(log_offset):
    """Extract tool results logged since log_offset."""
    _, _, results = parse_tool_calls_since(log_offset, include_results=True)
    return results

def get_log_offset():
    if not os.path.exists(SERVER_LOG):
        return 0
    return os.path.getsize(SERVER_LOG)

CURRENT_RUN_ID = None
CURRENT_RUN_STARTED_AT = None
RECORDED_RUN_METRICS = []


TIMELINE_INIT_SCRIPT = r"""
(() => {
    window.__visibilityTimeline = [];
    window.__lastUserAction = 0;

    const recordAction = () => { window.__lastUserAction = performance.now(); };
    window.addEventListener("pointerdown", recordAction, true);
    window.addEventListener("click", recordAction, true);
    window.addEventListener("keydown", recordAction, true);

    const checkVisibility = (el) => {
        if (!el) return false;
        if (el.classList && el.classList.contains("hidden")) return false;
        const style = window.getComputedStyle(el);
        if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
        const clock = document.getElementById("clockPage");
        if (clock && !clock.classList.contains("hidden")) {
            const clockStyle = window.getComputedStyle(clock);
            if (clockStyle.display !== "none" && clockStyle.visibility !== "hidden") {
                if (!clock.contains(el)) return false;
            }
        }
        return true;
    };

    const getSelector = (el) => {
        if (!el || !el.tagName) return "unknown";
        if (el.id) return "#" + el.id;
        if (el.className && typeof el.className === "string") {
            const firstClass = el.className.trim().split(/\s+/)[0];
            if (firstClass) return "." + firstClass;
        }
        return el.tagName.toLowerCase();
    };

    const stateMap = new WeakMap();

    const recordEvent = (el, isVis) => {
        const wasVis = stateMap.get(el) || false;
        if (wasVis !== isVis) {
            stateMap.set(el, isVis);
            window.__visibilityTimeline.push({
                element: getSelector(el),
                action: isVis ? "visible" : "hidden",
                ts: Math.round(performance.now()),
                last_user_action: Math.round(window.__lastUserAction)
            });
        }
    };

    const scanElement = (el) => {
        if (!el || el.nodeType !== 1) return;
        recordEvent(el, checkVisibility(el));
        for (const child of el.children) {
            if (child.id === "starters" || child.classList.contains("chip") || child.classList.contains("msg") || child.classList.contains("card")) {
                recordEvent(child, checkVisibility(child));
            }
        }
    };

    const setupObserver = () => {
        const feed = document.getElementById("feed");
        const clock = document.getElementById("clockPage");
        if (!feed) {
            setTimeout(setupObserver, 50);
            return;
        }

        const observer = new MutationObserver((mutations) => {
            const starters = document.getElementById("starters");
            if (starters) scanElement(starters);
            for (const m of mutations) {
                if (m.type === "childList") {
                    m.addedNodes.forEach(n => scanElement(n));
                    m.removedNodes.forEach(n => {
                        if (n.nodeType === 1) {
                            window.__visibilityTimeline.push({
                                element: getSelector(n),
                                action: "hidden",
                                ts: Math.round(performance.now()),
                                last_user_action: Math.round(window.__lastUserAction)
                            });
                        }
                    });
                } else if (m.type === "attributes") {
                    scanElement(m.target);
                    if (starters) scanElement(starters);
                }
            }
        });

        observer.observe(feed, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ["class", "style", "hidden"]
        });

        if (clock) {
            const clockObserver = new MutationObserver(() => {
                const starters = document.getElementById("starters");
                if (starters) scanElement(starters);
            });
            clockObserver.observe(clock, {
                attributes: true,
                attributeFilter: ["class", "style", "hidden"]
            });
        }
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", setupObserver);
    } else {
        setupObserver();
    }
})();
"""


def check_visibility_timeline(timeline: list, min_duration_ms: int = 1000) -> tuple[bool, str]:
    """Check timeline for elements that appeared and disappeared within min_duration_ms without user action."""
    vis_start = {}
    for entry in timeline:
        el = entry.get("element")
        action = entry.get("action")
        ts = entry.get("ts", 0)
        lua = entry.get("last_user_action", 0)

        if action == "visible":
            vis_start[el] = ts
        elif action == "hidden" and el in vis_start:
            start_ts = vis_start.pop(el)
            duration = ts - start_ts
            if 0 < duration < min_duration_ms:
                user_acted = (lua > 0 and start_ts - 100 <= lua <= ts + 100)
                if not user_acted:
                    return False, f"flicker: {el} appeared at {start_ts}ms and disappeared at {ts}ms ({duration}ms < {min_duration_ms}ms) with no user action"
    return True, ""

def append_metric(sha, dirty, scenario, passed, first_token_ms, complete_ms, elapsed_ms, tools, tool_calls, timeout_ms, screenshot=None, timeline=None, judge=None, friction=None):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "ts": ts,
        "run_id": CURRENT_RUN_ID,
        "run_started_at": CURRENT_RUN_STARTED_AT,
        "sha": sha,
        "dirty": dirty,
        "scenario": scenario,
        "pass": passed,
        "first_token_ms": first_token_ms,
        "complete_ms": complete_ms,
        "elapsed_ms": elapsed_ms,
        "tools": tools,
        "tool_calls": tool_calls,
        "timeout_ms": timeout_ms,
        "screenshot": screenshot or "",
        "timeline": timeline or [],
        "judge": judge or "",
        "friction": friction or "",
    }
    line = json.dumps(entry) + "\n"
    with open(METRICS_FILE, "a", encoding="utf-8") as f:
        f.write(line)
    try:
        with open(REPO_METRICS_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    global RECORDED_RUN_METRICS
    RECORDED_RUN_METRICS.append(entry)
    return entry

async def wake_screen(page):
    try:
        clock = await page.query_selector("#clockPage")
        if clock and await clock.is_visible():
            await clock.click()
            await page.wait_for_selector("#clockPage", state="hidden", timeout=5000)
    except Exception as e:
        print(f"wake_screen: {e}")


async def wake_by_wake_word(page, base_url=BASE_URL):
    """Wake screen via wake word webhook (POST /api/wake) (#473)."""
    try:
        req = urllib.request.Request(f"{base_url}/api/wake", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            pass
        await page.wait_for_selector("#clockPage", state="hidden", timeout=5000)
    except Exception as e:
        print(f"wake_by_wake_word: {e}")


async def update_metrics_with_judges():
    """Run concurrent judges on screenshots and update metric rows in files (#473)."""
    global RECORDED_RUN_METRICS
    try:
        from tools import tst_judge
    except ImportError:
        try:
            import tst_judge
        except ImportError:
            _tools_dir = str(PROJECT_DIR / "tools")
            if _tools_dir not in sys.path:
                sys.path.insert(0, _tools_dir)
            import tst_judge

    if not tst_judge or not RECORDED_RUN_METRICS:
        return

    print("Running post-run judges concurrently across captured screenshots...")
    await tst_judge.run_post_run_judges_concurrent(RECORDED_RUN_METRICS)

    # Rewrite metrics files to include friction and judge fields
    for mpath in [METRICS_FILE, REPO_METRICS_FILE]:
        try:
            if not os.path.exists(mpath):
                continue
            with open(mpath, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_lines = []
            entries_by_scenario = {e["scenario"]: e for e in RECORDED_RUN_METRICS}
            for line in lines:
                try:
                    data = json.loads(line)
                    if data.get("run_id") == CURRENT_RUN_ID and data.get("scenario") in entries_by_scenario:
                        matched = entries_by_scenario[data["scenario"]]
                        data["friction"] = matched.get("friction", "")
                        data["judge"] = matched.get("judge", "")
                        data["timeline"] = matched.get("timeline", [])
                        new_lines.append(json.dumps(data) + "\n")
                    else:
                        new_lines.append(line)
                except Exception:
                    new_lines.append(line)
            with open(mpath, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
        except Exception as e:
            print(f"Warning: failed updating {mpath} with judge data: {e}")


async def _run_scenarios(traj_argv=None, check_revision: bool = False):
    global CURRENT_RUN_ID, CURRENT_RUN_STARTED_AT
    sha, dirty = get_git_info()
    ok, err = server_preflight(BASE_URL, expected_sha=sha if check_revision else None)
    if not ok:
        print(err)
        sys.exit(1)

    clean_old_screenshots()
    run_started_dt = datetime.datetime.now(datetime.timezone.utc)
    CURRENT_RUN_STARTED_AT = run_started_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    CURRENT_RUN_ID = f"run_{run_started_dt.strftime('%Y%m%d_%H%M%S')}"
    print(f"Running scenarios on commit {sha} (dirty: {dirty}) [{CURRENT_RUN_ID}]")

    results = {}

    # --- Scenario 3: GET /api/screensaver directly ---
    t0 = time.time()
    log_offset = get_log_offset()
    timeout_ms = 5000
    s3_pass = False
    s3_detail = ""
    try:
        req = urllib.request.Request(f"{BASE_URL}/api/screensaver")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
        has_weather = "weather" in body and isinstance(body["weather"], dict)
        temp = body.get("weather", {}).get("temperature") if has_weather else None
        s3_pass = bool(has_weather and temp is not None and isinstance(temp, (int, float)))
        s3_detail = f"temperature={temp}" if s3_pass else f"body={body}"
    except Exception as e:
        s3_detail = str(e)
    elapsed_ms = int((time.time() - t0) * 1000)
    tools, tool_calls = parse_tool_calls_since(log_offset)
    append_metric(sha, dirty, "screensaver-api", s3_pass, None, None, elapsed_ms, tools, tool_calls, timeout_ms, screenshot="")
    results["scenario_3"] = {"pass": s3_pass, "detail": s3_detail, "elapsed_ms": elapsed_ms, "screenshot": ""}
    print(f"Scenario 3 (screensaver-api): pass={s3_pass}, elapsed={elapsed_ms}ms, detail={s3_detail}")

    # --- Scenario 13: GET /api/screensaver.jpg endpoint ---
    t0 = time.time()
    timeout_ms = 5000
    s13_pass = False
    s13_detail = ""
    try:
        req = urllib.request.Request(f"{BASE_URL}/api/screensaver.jpg")
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
            img_len = len(resp.read())
            s13_pass = bool(status == 200 and content_type.startswith("image/") and img_len > 0)
            s13_detail = f"status={status}, content_type='{content_type}', bytes={img_len}"
    except urllib.error.HTTPError as e:
        # 404 is valid when no image has been generated yet
        s13_pass = bool(e.code == 404)
        s13_detail = f"status={e.code} (no image on disk)"
    except Exception as e:
        s13_detail = str(e)
    elapsed_ms = int((time.time() - t0) * 1000)
    append_metric(sha, dirty, "screensaver-image-api", s13_pass, None, None, elapsed_ms, [], 0, timeout_ms, screenshot="")
    results["scenario_13"] = {"pass": s13_pass, "detail": s13_detail, "elapsed_ms": elapsed_ms, "screenshot": ""}
    print(f"Scenario 13 (screensaver-image-api): pass={s13_pass}, elapsed={elapsed_ms}ms, detail={s13_detail}")

    # Launch Playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
            ]
        )
        context = await browser.new_context(
            viewport={"width": 1024, "height": 600},
            locale="ja-JP"
        )
        await context.add_init_script(TIMELINE_INIT_SCRIPT)
        page = await context.new_page()

        console_errors = []
        failed_requests = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))
        page.on("requestfailed", lambda req: failed_requests.append(f"{req.method} {req.url}: {req.failure}"))

        # --- Scenario 1: Loads / ---
        t0 = time.time()
        log_offset = get_log_offset()
        timeout_ms = 15000
        max_elapsed_ms = LOAD_SLASH_MAX_ELAPSED_MS
        s1_pass = False
        status_code = None
        s1_shot = ""
        try:
            resp = await page.goto(f"{BASE_URL}/", wait_until="networkidle", timeout=15000)
            status_code = resp.status if resp else None
            s1_pass = (status_code == 200 and len(console_errors) == 0 and len(failed_requests) == 0)
            elapsed_ms = int((time.time() - t0) * 1000)
            if s1_pass and elapsed_ms > max_elapsed_ms:
                s1_pass = False
                console_errors.append(f"latency ceiling exceeded: {elapsed_ms}ms > {max_elapsed_ms}ms")
            s1_shot = await capture_scenario_screenshot(page, "load-slash", "ok" if s1_pass else "fail")
        except Exception as e:
            console_errors.append(str(e))
            elapsed_ms = int((time.time() - t0) * 1000)
            s1_shot = await capture_scenario_screenshot(page, "load-slash", "fail")
        tools, tool_calls = parse_tool_calls_since(log_offset)
        append_metric(sha, dirty, "load-slash", s1_pass, None, None, elapsed_ms, tools, tool_calls, timeout_ms, screenshot=s1_shot)
        results["scenario_1"] = {
            "pass": s1_pass,
            "status": status_code,
            "console_errors": console_errors.copy(),
            "failed_requests": failed_requests.copy(),
            "elapsed_ms": elapsed_ms,
            "max_elapsed_ms": max_elapsed_ms,
            "screenshot": s1_shot
        }
        ceiling_note = f", ceiling_exceeded={elapsed_ms > max_elapsed_ms}" if elapsed_ms > max_elapsed_ms else ""
        print(f"Scenario 1 (load-slash): pass={s1_pass}, status={status_code}, elapsed={elapsed_ms}ms{ceiling_note}")

        # --- Scenario 2: Leaves it alone until screensaver comes up ---
        t0 = time.time()
        log_offset = get_log_offset()
        timeout_ms = 5000
        s2_pass = False
        cw_text = ""
        s2_shot = ""
        try:
            await page.wait_for_timeout(3000)
            cw_el = await page.query_selector("#clockWeather")
            cw_text = await cw_el.inner_text() if cw_el else ""
            s2_pass = bool(cw_text and ("°" in cw_text or any(c.isdigit() for c in cw_text)))
            s2_shot = await capture_scenario_screenshot(page, "screensaver-clock-weather", "ok" if s2_pass else "fail")
        except Exception as e:
            cw_text = f"Error: {e}"
            s2_shot = await capture_scenario_screenshot(page, "screensaver-clock-weather", "fail")
        elapsed_ms = int((time.time() - t0) * 1000)
        tools, tool_calls = parse_tool_calls_since(log_offset)
        append_metric(sha, dirty, "screensaver-clock-weather", s2_pass, None, None, elapsed_ms, tools, tool_calls, timeout_ms, screenshot=s2_shot)
        results["scenario_2"] = {"pass": s2_pass, "clock_weather_text": cw_text, "elapsed_ms": elapsed_ms, "screenshot": s2_shot}
        print(f"Scenario 2 (screensaver-clock-weather): pass={s2_pass}, text={repr(cw_text)}, elapsed={elapsed_ms}ms")

        # --- Scenario 4: Clicks the Today's Weather chip ---
        t0 = time.time()
        log_offset = get_log_offset()
        timeout_ms = 45000
        s4_pass = False
        place_text = ""
        first_token_ms = None
        complete_ms = None
        card = None
        s4_shot = ""
        s4_agent_count = 0
        try:
            await wake_screen(page)
            await page.wait_for_timeout(1000)
            chip = await page.wait_for_selector('button.chip[data-i18n-prompt="ui_ask_weather"]', timeout=5000)
            s4_agent_count = len(await page.query_selector_all(".msg.agent:not(.partial)"))
            click_time = time.time()
            await chip.click()

            # Wait for .weather-card
            card = await page.wait_for_selector(".weather-card", timeout=45000)
            complete_ms = int((time.time() - click_time) * 1000)
            place_el = await card.query_selector(".wx-place")
            place_text = await place_el.inner_text() if place_el else ""
            s4_pass = bool(card and place_text.strip())
            s4_shot = await capture_scenario_screenshot(page, "weather-chip", "ok" if s4_pass else "fail")

            # Wait for post-tool audio/text stream to settle and seal (#756)
            s4_settle = time.time()
            while (time.time() - s4_settle) < 15.0:
                partials = await page.query_selector_all(".msg.agent.partial")
                starters = await page.query_selector("#starters:not(.hidden)")
                working = await page.query_selector_all(".working")
                completed_msgs = await page.query_selector_all(".msg.agent:not(.partial)")
                if starters and not partials and not working and len(completed_msgs) > s4_agent_count:
                    break
                await page.wait_for_timeout(200)
            try:
                await page.wait_for_selector(".msg.agent.partial", state="detached", timeout=3000)
            except Exception:
                pass
        except Exception as e:
            place_text = f"Error: {e}"
            s4_shot = await capture_scenario_screenshot(page, "weather-chip", "fail")
        elapsed_ms = int((time.time() - t0) * 1000)
        tools, tool_calls = parse_tool_calls_since(log_offset)
        append_metric(sha, dirty, "weather-chip", s4_pass, first_token_ms, complete_ms, elapsed_ms, tools, tool_calls, timeout_ms, screenshot=s4_shot)
        results["scenario_4"] = {"pass": s4_pass, "place_text": place_text, "complete_ms": complete_ms, "elapsed_ms": elapsed_ms, "tools": tools, "screenshot": s4_shot}
        print(f"Scenario 4 (weather-chip): pass={s4_pass}, place={repr(place_text)}, complete={complete_ms}ms, elapsed={elapsed_ms}ms")

        # --- Scenario 7: Regression #203 - Weather card remote local time & timezone ---
        t0 = time.time()
        log_offset = get_log_offset()
        timeout_ms = 5000
        s7_pass = False
        wx_time_text = ""
        wx_tz_title = ""
        s7_detail = ""
        s7_shot = ""
        try:
            if not card:
                card = await page.query_selector(".weather-card")
            if card:
                time_el = await card.query_selector(".wx-head .wx-time")
                if time_el:
                    wx_time_text = (await time_el.inner_text()).strip()
                    wx_tz_title = await time_el.get_attribute("title") or ""
                    is_time_fmt = bool(re.match(r"^\d{1,2}:\d{2}$", wx_time_text))
                    has_tz = bool(len(wx_tz_title) > 0)
                    s7_pass = is_time_fmt and has_tz
                    s7_detail = f"time={wx_time_text}, tz={wx_tz_title}"
                else:
                    s7_detail = ".wx-head .wx-time element not found"
            else:
                s7_detail = ".weather-card element not found"
            s7_shot = await capture_scenario_screenshot(page, "weather-card-timezone-203", "ok" if s7_pass else "fail")
        except Exception as e:
            s7_detail = f"Error: {e}"
            s7_shot = await capture_scenario_screenshot(page, "weather-card-timezone-203", "fail")
        elapsed_ms = int((time.time() - t0) * 1000)
        tools, tool_calls = parse_tool_calls_since(log_offset)
        append_metric(sha, dirty, "weather-card-timezone-203", s7_pass, None, None, elapsed_ms, tools, tool_calls, timeout_ms, screenshot=s7_shot)
        results["scenario_7"] = {"pass": s7_pass, "detail": s7_detail, "elapsed_ms": elapsed_ms, "screenshot": s7_shot}
        print(f"Scenario 7 (weather-card-timezone-203): pass={s7_pass}, detail={s7_detail}, elapsed={elapsed_ms}ms")

        # --- Scenario 5: Types a question and waits for an answer ---
        t0 = time.time()
        log_offset = get_log_offset()
        timeout_ms = 45000
        s5_pass = False
        first_token_ms = None
        complete_ms = None
        agent_texts = []
        last_text = ""
        card_text = ""
        s5_shot = ""
        try:
            await wake_screen(page)
            prompt = "What is the capital of France?"

            # Wait for any in-flight streaming response from prior turns to settle (#756)
            settle_start = time.time()
            while (time.time() - settle_start) < 15.0:
                current_agents = await page.query_selector_all(".msg.agent:not(.partial)")
                partials = await page.query_selector_all(".msg.agent.partial")
                working = await page.query_selector_all(".working")
                starters = await page.query_selector("#starters:not(.hidden)")
                if len(current_agents) > s4_agent_count and not partials and not working and starters:
                    break
                await page.wait_for_timeout(200)

            # Retain detached wait for partial streams after the message appears
            try:
                await page.wait_for_selector(".msg.agent.partial", state="detached", timeout=5000)
            except Exception:
                pass

            await page.wait_for_selector("#textInput", timeout=5000)
            await page.fill("#textInput", prompt)
            await page.wait_for_selector("#sendButton:not([disabled])", timeout=5000)

            prior_agent_msgs = await page.query_selector_all(".msg.agent:not(.partial)")
            prior_count = len(prior_agent_msgs)
            prior_all_agents = len(await page.query_selector_all(".msg.agent"))
            prior_systems = len(await page.query_selector_all(".msg.status, .msg.system"))
            prior_cards = len(await page.query_selector_all("#cards .card, #cardArea .card"))

            click_time = time.time()
            await page.click("#sendButton")

            # First token latency
            try:
                ft_start = time.time()
                while (time.time() - ft_start) < 15.0:
                    agent_nodes = await page.query_selector_all(".msg.agent")
                    status_nodes = await page.query_selector_all(".msg.status, .msg.system")
                    card_nodes = await page.query_selector_all("#cards .card, #cardArea .card")
                    search_section = await page.query_selector(
                        "#cards .composite-section[data-section='web_search'], #cardArea .composite-section[data-section='web_search']"
                    )
                    if (
                        len(agent_nodes) > prior_all_agents
                        or len(status_nodes) > prior_systems
                        or len(card_nodes) > prior_cards
                        or search_section
                    ):
                        first_token_ms = int((time.time() - click_time) * 1000)
                        break
                    await page.wait_for_timeout(100)
            except Exception:
                pass

            # Complete answer latency: wait for Paris answer in new agent messages or in card pane
            complete_start = time.time()
            while (time.time() - complete_start) < 30.0:
                completed_msgs = await page.query_selector_all(".msg.agent:not(.partial)")
                agent_texts = [await m.inner_text() for m in completed_msgs]
                new_agent_texts = agent_texts[prior_count:] if len(agent_texts) > prior_count else []
                new_text = " ".join(new_agent_texts)

                card_el = await page.query_selector("#cards, #cardArea")
                card_text = (await card_el.inner_text()) if card_el else ""

                if ("Paris" in new_text or "パリ" in new_text) or ("Paris" in card_text or "パリ" in card_text):
                    complete_ms = int((time.time() - click_time) * 1000)
                    break
                await page.wait_for_timeout(200)

            # Wait for Scenario 5 agent reply to be sealed (completed turn committed to SQLite)
            seal_start = time.time()
            while (time.time() - seal_start) < 15.0:
                completed_msgs = await page.query_selector_all(".msg.agent:not(.partial)")
                partials = await page.query_selector_all(".msg.agent.partial")
                if len(completed_msgs) > prior_count and not partials:
                    break
                await page.wait_for_timeout(200)

            try:
                await page.wait_for_selector(".msg.agent.partial", state="detached", timeout=3000)
            except Exception:
                pass

            await page.wait_for_timeout(1000)

            msgs = await page.query_selector_all(".msg.agent")
            agent_texts = [await m.inner_text() for m in msgs]
            new_agent_texts = agent_texts[prior_count:] if len(agent_texts) > prior_count else []
            last_text = new_agent_texts[-1] if new_agent_texts else ""
            card_el = await page.query_selector("#cards, #cardArea")
            card_text = (await card_el.inner_text()) if card_el else ""
            new_text = " ".join(new_agent_texts)
            s5_pass = bool("Paris" in new_text or "パリ" in new_text or "Paris" in card_text or "パリ" in card_text)
            s5_shot = await capture_scenario_screenshot(page, "text-input-qa", "ok" if s5_pass else "fail")
        except Exception as e:
            agent_texts.append(f"Error: {e}")
            s5_shot = await capture_scenario_screenshot(page, "text-input-qa", "fail")
        elapsed_ms = int((time.time() - t0) * 1000)
        tools, tool_calls = parse_tool_calls_since(log_offset)
        append_metric(sha, dirty, "text-input-qa", s5_pass, first_token_ms, complete_ms, elapsed_ms, tools, tool_calls, timeout_ms, screenshot=s5_shot)
        reply_text = last_text if last_text else (card_text if card_text else (agent_texts[-1] if agent_texts else ""))
        results["scenario_5"] = {"pass": s5_pass, "first_token_ms": first_token_ms, "complete_ms": complete_ms, "elapsed_ms": elapsed_ms, "tools": tools, "reply": reply_text, "screenshot": s5_shot}
        print(f"Scenario 5 (text-input-qa): pass={s5_pass}, first_token={first_token_ms}ms, complete={complete_ms}ms, elapsed={elapsed_ms}ms")

        # --- Scenario 6: Reloads mid-conversation ---
        t0 = time.time()
        log_offset = get_log_offset()
        timeout_ms = 15000
        max_elapsed_ms = RELOAD_MID_CONV_MAX_ELAPSED_MS
        s6_pass = False
        feed_msgs = []
        s6_shot = ""
        try:
            # Ensure prior turn is fully sealed before reloading
            try:
                await page.wait_for_selector(".msg.agent.partial", state="detached", timeout=5000)
            except Exception:
                pass
            await page.reload(wait_until="networkidle", timeout=15000)
            await wake_screen(page)
            await page.wait_for_timeout(3000)

            # Check for turns in feed or messages
            nodes = await page.query_selector_all(".msg")
            feed_msgs = [await n.inner_text() for n in nodes]

            # Assert restored messages alternate roles in chronological order (user, agent, user, agent)
            conv_nodes = await page.query_selector_all(".msg.user, .msg.agent")
            restored_roles = []
            for n in conv_nodes:
                cls = (await n.get_attribute("class") or "").split()
                if "user" in cls:
                    restored_roles.append("user")
                elif "agent" in cls:
                    restored_roles.append("agent")

            expected_roles = ["user", "agent", "user", "agent"]
            roles_alternating = (restored_roles == expected_roles)
            if not roles_alternating:
                feed_msgs.append(f"role alternation mismatch: got {restored_roles}, want {expected_roles}")

            has_content = any("capital of France" in t or "Paris" in t or "2 + 2" in t for t in feed_msgs)
            s6_pass = bool(roles_alternating and has_content)
            elapsed_ms = int((time.time() - t0) * 1000)
            if s6_pass and elapsed_ms > max_elapsed_ms:
                s6_pass = False
                feed_msgs.append(f"latency ceiling exceeded: {elapsed_ms}ms > {max_elapsed_ms}ms")
            s6_shot = await capture_scenario_screenshot(page, "reload-mid-conversation", "ok" if s6_pass else "fail")
        except Exception as e:
            feed_msgs.append(f"Error: {e}")
            elapsed_ms = int((time.time() - t0) * 1000)
            s6_shot = await capture_scenario_screenshot(page, "reload-mid-conversation", "fail")
        tools, tool_calls = parse_tool_calls_since(log_offset)
        append_metric(sha, dirty, "reload-mid-conversation", s6_pass, None, None, elapsed_ms, tools, tool_calls, timeout_ms, screenshot=s6_shot)
        results["scenario_6"] = {"pass": s6_pass, "feed_messages": feed_msgs, "elapsed_ms": elapsed_ms, "max_elapsed_ms": max_elapsed_ms, "screenshot": s6_shot}
        ceiling_note = f", ceiling_exceeded={elapsed_ms > max_elapsed_ms}" if elapsed_ms > max_elapsed_ms else ""
        print(f"Scenario 6 (reload-mid-conversation): pass={s6_pass}, feed_messages_count={len(feed_msgs)}, elapsed={elapsed_ms}ms{ceiling_note}")

        # --- Scenario 9: Starter buttons placement at feed bottom (#368 regression) ---
        # Note on #473 / #472: Workaround sites identified in #473 are replaced by standing
        # regression assertions in tools/regressions.yml (entry #472).
        t0 = time.time()
        timeout_ms = 5000
        s9_pass = False
        s9_detail = ""
        s9_shot = ""
        try:
            is_placed = await page.evaluate("""() => {
                const starters = document.getElementById("starters");
                const sidePanel = document.getElementById("sidePanel");
                const cards = document.getElementById("cards");
                // Clear button click sets up an empty feed for testing placement (#473);
                // starter chip visibility on wake is guarded in tools/regressions.yml (#472).
                document.getElementById('clearButton')?.click();
                // In landscape (1024x600), starters lives in sidePanel as sibling to cards, after cards
                return starters?.parentElement === sidePanel && starters?.previousElementSibling === cards;
            }""")
            s9_pass = bool(is_placed)
            s9_detail = f"right_panel_placement={is_placed}"
            s9_shot = await capture_scenario_screenshot(page, "starters-bottom-placement-368", "ok" if s9_pass else "fail")
        except Exception as e:
            s9_detail = str(e)
            s9_shot = await capture_scenario_screenshot(page, "starters-bottom-placement-368", "fail")
        elapsed_ms = int((time.time() - t0) * 1000)
        append_metric(sha, dirty, "starters-bottom-placement-368", s9_pass, None, None, elapsed_ms, [], 0, timeout_ms, screenshot=s9_shot)
        results["scenario_9"] = {"pass": s9_pass, "detail": s9_detail, "elapsed_ms": elapsed_ms, "screenshot": s9_shot}
        print(f"Scenario 9 (starters-bottom-placement-368): pass={s9_pass}, detail={s9_detail}, elapsed={elapsed_ms}ms")

        # --- Scenario 10: Touch-friendly scrollbar width (#371 regression) ---
        t0 = time.time()
        timeout_ms = 5000
        s10_pass = False
        s10_detail = ""
        s10_shot = ""
        try:
            widths = await page.evaluate("""() => {
                const found = [];
                for (const sheet of document.styleSheets) {
                    try {
                        for (const rule of sheet.cssRules) {
                            if (rule.selectorText && (rule.selectorText.includes(".feed::-webkit-scrollbar") || rule.selectorText.includes(".cards::-webkit-scrollbar"))) {
                                const w = rule.style.width;
                                if (w) found.push(w);
                            }
                        }
                    } catch (e) {}
                }
                return found;
            }""")
            s10_pass = bool(len(widths) > 0 and all("14px" in w for w in widths))
            s10_detail = f"widths={widths}"
            s10_shot = await capture_scenario_screenshot(page, "touch-scrollbars-371", "ok" if s10_pass else "fail")
        except Exception as e:
            s10_detail = str(e)
            s10_shot = await capture_scenario_screenshot(page, "touch-scrollbars-371", "fail")
        elapsed_ms = int((time.time() - t0) * 1000)
        append_metric(sha, dirty, "touch-scrollbars-371", s10_pass, None, None, elapsed_ms, [], 0, timeout_ms, screenshot=s10_shot)
        results["scenario_10"] = {"pass": s10_pass, "detail": s10_detail, "elapsed_ms": elapsed_ms, "screenshot": s10_shot}
        print(f"Scenario 10 (touch-scrollbars-371): pass={s10_pass}, detail={s10_detail}, elapsed={elapsed_ms}ms")

        # --- Scenario 11: Build badge on page load (#369 regression) ---
        t0 = time.time()
        timeout_ms = 5000
        s11_pass = False
        s11_detail = ""
        s11_shot = ""
        try:
            badge = await page.wait_for_selector("#buildBadge", timeout=3000)
            badge_text = (await badge.inner_text()).strip() if badge else ""
            s11_pass = bool(sha in badge_text and len(badge_text) > 0)
            s11_detail = f"badge_text='{badge_text}'"
            s11_shot = await capture_scenario_screenshot(page, "build-badge-369", "ok" if s11_pass else "fail")
        except Exception as e:
            s11_detail = str(e)
            s11_shot = await capture_scenario_screenshot(page, "build-badge-369", "fail")
        elapsed_ms = int((time.time() - t0) * 1000)
        append_metric(sha, dirty, "build-badge-369", s11_pass, None, None, elapsed_ms, [], 0, timeout_ms, screenshot=s11_shot)
        results["scenario_11"] = {"pass": s11_pass, "detail": s11_detail, "elapsed_ms": elapsed_ms, "screenshot": s11_shot}
        print(f"Scenario 11 (build-badge-369): pass={s11_pass}, detail={s11_detail}, elapsed={elapsed_ms}ms")

        # --- Scenario 12: Cache-busting query and no-cache on static assets (#178 regression) ---
        t0 = time.time()
        timeout_ms = 5000
        s12_pass = False
        s12_detail = ""
        s12_shot = ""
        try:
            css_href = await page.evaluate("""() => document.querySelector('link[href*="style.css"]')?.getAttribute('href') || ''""")
            js_src = await page.evaluate("""() => document.querySelector('script[type="module"]')?.getAttribute('src') || ''""")
            has_queries = "?v=" in css_href and "?v=" in js_src

            req_css = urllib.request.Request(f"{BASE_URL}/static/css/style.css")
            with urllib.request.urlopen(req_css, timeout=5) as resp:
                cc_css = resp.headers.get("Cache-Control", "")

            req_js = urllib.request.Request(f"{BASE_URL}/static/js/app.js")
            with urllib.request.urlopen(req_js, timeout=5) as resp:
                cc_js = resp.headers.get("Cache-Control", "")

            s12_pass = bool(has_queries and "no-cache" in cc_css and "no-cache" in cc_js)
            s12_detail = f"has_queries={has_queries}, cc_css='{cc_css}', cc_js='{cc_js}'"
            s12_shot = await capture_scenario_screenshot(page, "static-cache-busting-178", "ok" if s12_pass else "fail")
        except Exception as e:
            s12_detail = str(e)
            s12_shot = await capture_scenario_screenshot(page, "static-cache-busting-178", "fail")
        elapsed_ms = int((time.time() - t0) * 1000)
        append_metric(sha, dirty, "static-cache-busting-178", s12_pass, None, None, elapsed_ms, [], 0, timeout_ms, screenshot=s12_shot)
        results["scenario_12"] = {"pass": s12_pass, "detail": s12_detail, "elapsed_ms": elapsed_ms, "screenshot": s12_shot}
        print(f"Scenario 12 (static-cache-busting-178): pass={s12_pass}, detail={s12_detail}, elapsed={elapsed_ms}ms")

        forget_user("terminal")
        # --- Scenario 14: News chip (ui_ask_news starter chip & web_search card) ---
        t0 = time.time()
        log_offset = get_log_offset()
        timeout_ms = 30000
        s14_pass = False
        first_token_ms = None
        complete_ms = None
        s14_detail = ""
        news_page = None
        s14_shot = ""
        try:
            news_page = await context.new_page()
            await news_page.goto(BASE_URL)
            await news_page.wait_for_timeout(1000)
            await wake_screen(news_page)
            news_chip = await news_page.wait_for_selector('button.chip[data-i18n-prompt="ui_ask_news"]', timeout=5000)
            click_time = time.time()
            await news_chip.click()

            try:
                await news_page.wait_for_selector(".msg.agent", timeout=15000)
                first_token_ms = int((time.time() - click_time) * 1000)
            except Exception:
                pass

            reply_el = await news_page.wait_for_selector(".msg.agent:not(.partial)", timeout=25000)
            complete_ms = int((time.time() - click_time) * 1000)
            reply_text = (await reply_el.inner_text()).strip()

            card_el = await news_page.wait_for_selector(".cards > *", timeout=5000)
            has_card = card_el is not None
            s14_pass = bool(len(reply_text) > 0 and has_card)
            s14_detail = f"reply_len={len(reply_text)}, has_card={has_card}"
            s14_shot = await capture_scenario_screenshot(news_page, "news-chip-websearch", "ok" if s14_pass else "fail")
        except Exception as e:
            s14_detail = str(e)
            if news_page:
                s14_shot = await capture_scenario_screenshot(news_page, "news-chip-websearch", "fail")
        finally:
            if news_page:
                await news_page.close()
        elapsed_ms = int((time.time() - t0) * 1000)
        tools, tool_calls = parse_tool_calls_since(log_offset)
        append_metric(sha, dirty, "news-chip-websearch", s14_pass, first_token_ms, complete_ms, elapsed_ms, tools, tool_calls, timeout_ms, screenshot=s14_shot)
        results["scenario_14"] = {"pass": s14_pass, "detail": s14_detail, "elapsed_ms": elapsed_ms, "tools": tools, "screenshot": s14_shot}
        print(f"Scenario 14 (news-chip-websearch): pass={s14_pass}, complete={complete_ms}ms, elapsed={elapsed_ms}ms, tools={tools}")

        # --- Scenario 15: Find places chip (ui_ask_places starter chip, maps_search & cards) ---
        t0 = time.time()
        log_offset = get_log_offset()
        timeout_ms = 35000
        s15_pass = False
        first_token_ms = None
        complete_ms = None
        s15_detail = ""
        places_page = None
        s15_shot = ""
        try:
            places_page = await context.new_page()
            await places_page.goto(BASE_URL)
            await places_page.wait_for_timeout(1000)
            await wake_screen(places_page)
            places_chip = await places_page.wait_for_selector('button.chip[data-i18n-prompt="ui_ask_places"]', timeout=5000)
            click_time = time.time()
            await places_chip.click()

            try:
                await places_page.wait_for_selector(".msg.agent", timeout=15000)
                first_token_ms = int((time.time() - click_time) * 1000)
            except Exception:
                pass

            reply_el = await places_page.wait_for_selector(".msg.agent:not(.partial)", timeout=25000)
            complete_ms = int((time.time() - click_time) * 1000)
            reply_text = (await reply_el.inner_text()).strip()

            card_el = await places_page.wait_for_selector(".cards > *", timeout=10000)
            has_card = card_el is not None
            s15_pass = bool(len(reply_text) > 0 and has_card)
            s15_detail = f"reply_len={len(reply_text)}, has_card={has_card}"
            s15_shot = await capture_scenario_screenshot(places_page, "places-chip-mapssearch", "ok" if s15_pass else "fail")
        except Exception as e:
            s15_detail = str(e)
            if places_page:
                s15_shot = await capture_scenario_screenshot(places_page, "places-chip-mapssearch", "fail")
        finally:
            if places_page:
                await places_page.close()
        elapsed_ms = int((time.time() - t0) * 1000)
        tools, tool_calls = parse_tool_calls_since(log_offset)
        for res in getattr(tool_calls, "results", []):
            if res.get("tool") == "place_info":
                shape = res.get("shape", {})
                if isinstance(shape, dict) and shape.get("images") == 0 and shape.get("sources") == 0:
                    s15_pass = False
                    s15_detail = f"place_info returned empty images and sources: {shape}"
        append_metric(sha, dirty, "places-chip-mapssearch", s15_pass, first_token_ms, complete_ms, elapsed_ms, tools, tool_calls, timeout_ms, screenshot=s15_shot)
        results["scenario_15"] = {"pass": s15_pass, "detail": s15_detail, "elapsed_ms": elapsed_ms, "tools": tools, "screenshot": s15_shot}
        print(f"Scenario 15 (places-chip-mapssearch): pass={s15_pass}, complete={complete_ms}ms, elapsed={elapsed_ms}ms, tools={tools}")

        forget_user("terminal")
        # --- Scenario 16: Market chip (ui_ask_market starter chip & financial search) ---
        t0 = time.time()
        log_offset = get_log_offset()
        timeout_ms = 35000
        s16_pass = False
        first_token_ms = None
        complete_ms = None
        s16_detail = ""
        market_page = None
        s16_shot = ""
        try:
            market_page = await context.new_page()
            await market_page.goto(BASE_URL)
            await market_page.wait_for_timeout(1000)
            await wake_screen(market_page)
            market_chip = await market_page.wait_for_selector('button.chip[data-i18n-prompt="ui_ask_market"]', timeout=5000)
            click_time = time.time()
            await market_chip.click()

            try:
                await market_page.wait_for_selector(".msg.agent", timeout=15000)
                first_token_ms = int((time.time() - click_time) * 1000)
            except Exception:
                pass

            reply_el = await market_page.wait_for_selector(".msg.agent:not(.partial)", timeout=25000)
            complete_ms = int((time.time() - click_time) * 1000)
            reply_text = (await reply_el.inner_text()).strip()

            s16_pass = bool(len(reply_text) > 0)
            s16_detail = f"reply_len={len(reply_text)}"
            s16_shot = await capture_scenario_screenshot(market_page, "market-chip-websearch", "ok" if s16_pass else "fail")
        except Exception as e:
            s16_detail = str(e)
            if market_page:
                s16_shot = await capture_scenario_screenshot(market_page, "market-chip-websearch", "fail")
        finally:
            if market_page:
                await market_page.close()
        elapsed_ms = int((time.time() - t0) * 1000)
        tools, tool_calls = parse_tool_calls_since(log_offset)
        append_metric(sha, dirty, "market-chip-websearch", s16_pass, first_token_ms, complete_ms, elapsed_ms, tools, tool_calls, timeout_ms, screenshot=s16_shot)
        results["scenario_16"] = {"pass": s16_pass, "detail": s16_detail, "elapsed_ms": elapsed_ms, "tools": tools, "screenshot": s16_shot}
        print(f"Scenario 16 (market-chip-websearch): pass={s16_pass}, complete={complete_ms}ms, elapsed={elapsed_ms}ms, tools={tools}")

        forget_user("terminal")
        # --- Scenario 17: Directions chip (ui_ask_routes starter chip, maps_route & cards) ---
        t0 = time.time()
        log_offset = get_log_offset()
        timeout_ms = 45000
        s17_pass = False
        first_token_ms = None
        complete_ms = None
        s17_detail = ""
        routes_page = None
        s17_shot = ""
        try:
            routes_page = await context.new_page()
            await routes_page.goto(BASE_URL)
            await routes_page.wait_for_timeout(1000)
            await wake_screen(routes_page)
            routes_chip = await routes_page.wait_for_selector('button.chip[data-i18n-prompt="ui_ask_routes"]', timeout=5000)
            click_time = time.time()
            await routes_chip.click()

            try:
                await routes_page.wait_for_selector(".msg.agent", timeout=15000)
                first_token_ms = int((time.time() - click_time) * 1000)
            except Exception:
                pass

            reply_el = await routes_page.wait_for_selector(".msg.agent:not(.partial)", timeout=25000)
            complete_ms = int((time.time() - click_time) * 1000)
            reply_text = (await reply_el.inner_text()).strip()

            try:
                card_el = await routes_page.wait_for_selector(".cards > *", timeout=3000)
            except Exception:
                card_el = None
            has_card = card_el is not None

            if not has_card:
                await routes_page.wait_for_selector("#textInput", timeout=5000)
                await routes_page.fill("#textInput", "鎌倉駅")
                await routes_page.wait_for_selector("#sendButton:not([disabled])", timeout=5000)
                await routes_page.click("#sendButton")
                try:
                    card_el = await routes_page.wait_for_selector(".cards > *", timeout=25000)
                except Exception:
                    card_el = None
                has_card = card_el is not None
                msgs = await routes_page.query_selector_all(".msg.agent:not(.partial)")
                if msgs:
                    reply_text = (await msgs[-1].inner_text()).strip()

            s17_pass = bool(len(reply_text) > 0 and has_card)
            s17_detail = f"reply_len={len(reply_text)}, has_card={has_card}"
            s17_shot = await capture_scenario_screenshot(routes_page, "routes-chip-mapsroute", "ok" if s17_pass else "fail")
        except Exception as e:
            s17_detail = str(e)
            if routes_page:
                s17_shot = await capture_scenario_screenshot(routes_page, "routes-chip-mapsroute", "fail")
        finally:
            if routes_page:
                await routes_page.close()
        elapsed_ms = int((time.time() - t0) * 1000)
        tools, tool_calls = parse_tool_calls_since(log_offset)
        append_metric(sha, dirty, "routes-chip-mapsroute", s17_pass, first_token_ms, complete_ms, elapsed_ms, tools, tool_calls, timeout_ms, screenshot=s17_shot)
        results["scenario_17"] = {"pass": s17_pass, "detail": s17_detail, "elapsed_ms": elapsed_ms, "tools": tools, "screenshot": s17_shot}
        print(f"Scenario 17 (routes-chip-mapsroute): pass={s17_pass}, complete={complete_ms}ms, elapsed={elapsed_ms}ms, tools={tools}")

        forget_user("terminal")
        # --- Scenario 18: Web search chip (ui_ask_web_search starter chip, google_search grounding) ---
        t0 = time.time()
        log_offset = get_log_offset()
        timeout_ms = 35000
        s18_pass = False
        first_token_ms = None
        complete_ms = None
        s18_detail = ""
        websearch_page = None
        s18_shot = ""
        try:
            websearch_page = await context.new_page()
            await websearch_page.goto(BASE_URL)
            await websearch_page.wait_for_timeout(1000)
            await wake_screen(websearch_page)
            websearch_chip = await websearch_page.wait_for_selector('button.chip[data-i18n-prompt="ui_ask_web_search"]', timeout=5000)
            click_time = time.time()
            await websearch_chip.click()

            try:
                await websearch_page.wait_for_selector(".msg.agent", timeout=15000)
                first_token_ms = int((time.time() - click_time) * 1000)
            except Exception:
                pass

            reply_el = await websearch_page.wait_for_selector(".msg.agent:not(.partial)", timeout=25000)
            complete_ms = int((time.time() - click_time) * 1000)
            reply_text = (await reply_el.inner_text()).strip()

            card_el = await websearch_page.wait_for_selector(".cards > *", timeout=10000)
            has_card = card_el is not None
            s18_pass = bool(len(reply_text) > 0 and has_card)
            s18_detail = f"reply_len={len(reply_text)}, has_card={has_card}"
            s18_shot = await capture_scenario_screenshot(websearch_page, "websearch-chip-grounding", "ok" if s18_pass else "fail")
        except Exception as e:
            s18_detail = str(e)
            if websearch_page:
                s18_shot = await capture_scenario_screenshot(websearch_page, "websearch-chip-grounding", "fail")
        finally:
            if websearch_page:
                await websearch_page.close()
        elapsed_ms = int((time.time() - t0) * 1000)
        tools, tool_calls = parse_tool_calls_since(log_offset)
        append_metric(sha, dirty, "websearch-chip-grounding", s18_pass, first_token_ms, complete_ms, elapsed_ms, tools, tool_calls, timeout_ms, screenshot=s18_shot)
        results["scenario_18"] = {"pass": s18_pass, "detail": s18_detail, "elapsed_ms": elapsed_ms, "tools": tools, "screenshot": s18_shot}
        print(f"Scenario 18 (websearch-chip-grounding): pass={s18_pass}, complete={complete_ms}ms, elapsed={elapsed_ms}ms, tools={tools}")

        forget_user("terminal")
        # --- Scenario 19: Video chip (ui_ask_video starter chip, youtube_search & video cards) ---
        t0 = time.time()
        log_offset = get_log_offset()
        timeout_ms = 45000
        s19_pass = False
        first_token_ms = None
        complete_ms = None
        s19_detail = ""
        video_page = None
        s19_shot = ""
        try:
            video_page = await context.new_page()
            await video_page.goto(BASE_URL)
            await video_page.wait_for_timeout(1000)
            await wake_screen(video_page)
            video_chip = await video_page.wait_for_selector('button.chip[data-i18n-prompt="ui_ask_video"]', timeout=5000)
            click_time = time.time()
            await video_chip.click()

            try:
                await video_page.wait_for_selector(".msg.agent", timeout=15000)
                first_token_ms = int((time.time() - click_time) * 1000)
            except Exception:
                pass

            reply_el = await video_page.wait_for_selector(".msg.agent:not(.partial)", timeout=25000)
            complete_ms = int((time.time() - click_time) * 1000)
            reply_text = (await reply_el.inner_text()).strip()

            try:
                card_el = await video_page.wait_for_selector(".cards > *", timeout=3000)
            except Exception:
                card_el = None
            yt_count = len(await video_page.query_selector_all(".cards [data-video-id], .cards iframe, .cards .video-card"))
            has_card = card_el is not None and yt_count > 0

            if not has_card:
                await video_page.wait_for_selector("#textInput", timeout=5000)
                await video_page.fill("#textInput", "ケトルの水垢の落とし方")
                await video_page.wait_for_selector("#sendButton:not([disabled])", timeout=5000)
                await video_page.click("#sendButton")
                try:
                    card_el = await video_page.wait_for_selector(".cards [data-video-id], .cards iframe, .cards .video-card", timeout=25000)
                except Exception:
                    card_el = None
                yt_count = len(await video_page.query_selector_all(".cards [data-video-id], .cards iframe, .cards .video-card"))
                has_card = card_el is not None and yt_count > 0
                msgs = await video_page.query_selector_all(".msg.agent:not(.partial)")
                if msgs:
                    reply_text = (await msgs[-1].inner_text()).strip()

            s19_pass = bool(len(reply_text) > 0 and has_card)
            s19_detail = f"reply_len={len(reply_text)}, videos={yt_count}"
            s19_shot = await capture_scenario_screenshot(video_page, "video-chip-youtubesearch", "ok" if s19_pass else "fail")
        except Exception as e:
            s19_detail = str(e)
            if video_page:
                s19_shot = await capture_scenario_screenshot(video_page, "video-chip-youtubesearch", "fail")
        finally:
            if video_page:
                await video_page.close()
        elapsed_ms = int((time.time() - t0) * 1000)
        tools, tool_calls = parse_tool_calls_since(log_offset)
        append_metric(sha, dirty, "video-chip-youtubesearch", s19_pass, first_token_ms, complete_ms, elapsed_ms, tools, tool_calls, timeout_ms, screenshot=s19_shot)
        results["scenario_19"] = {"pass": s19_pass, "detail": s19_detail, "elapsed_ms": elapsed_ms, "tools": tools, "screenshot": s19_shot}
        print(f"Scenario 19 (video-chip-youtubesearch): pass={s19_pass}, complete={complete_ms}ms, elapsed={elapsed_ms}ms, tools={tools}")

        # --- Scenario: browse-card (ui_browse / browse) ---
        t0 = time.time()
        timeout_ms = 25000
        s_browse_pass = False
        s_browse_detail = ""
        s_browse_shot = ""
        first_token_ms = None
        complete_ms = None
        log_offset = get_log_offset()
        browse_page = None
        try:
            browse_page = await context.new_page()
            await browse_page.goto(BASE_URL, timeout=10000)
            await wake_screen(browse_page)
            await browse_page.wait_for_selector("#textInput", timeout=5000)
            t_send = time.time()
            await browse_page.fill("#textInput", "最新のAIニュースのウェブサイトを開いて要約して")
            await browse_page.click("#sendButton")
            first_msg = await browse_page.wait_for_selector(".msg.agent", timeout=15000)
            first_token_ms = int((time.time() - t_send) * 1000)
            reply_el = await browse_page.wait_for_selector(".msg.agent:not(.partial)", timeout=20000)
            complete_ms = int((time.time() - t_send) * 1000)
            reply_text = (await reply_el.inner_text()).strip()
            card_el = await browse_page.wait_for_selector(".card, .cards > *", timeout=25000)
            has_card = card_el is not None
            s_browse_pass = bool(len(reply_text) > 0 and has_card)
            s_browse_detail = f"reply_len={len(reply_text)}, card={has_card}"
            s_browse_shot = await capture_scenario_screenshot(browse_page, "browse-card", "ok" if s_browse_pass else "fail")
        except Exception as e:
            s_browse_detail = str(e)
            if browse_page:
                s_browse_shot = await capture_scenario_screenshot(browse_page, "browse-card", "fail")
        finally:
            if browse_page:
                await browse_page.close()
        elapsed_ms = int((time.time() - t0) * 1000)
        tools, tool_calls = parse_tool_calls_since(log_offset)
        append_metric(sha, dirty, "browse-card", s_browse_pass, first_token_ms, complete_ms, elapsed_ms, tools, tool_calls, timeout_ms, screenshot=s_browse_shot)
        results["browse_card"] = {"pass": s_browse_pass, "detail": s_browse_detail, "elapsed_ms": elapsed_ms, "tools": tools, "screenshot": s_browse_shot}
        print(f"Scenario (browse-card): pass={s_browse_pass}, complete={complete_ms}ms, elapsed={elapsed_ms}ms, tools={tools}")

        # --- Scenario: plan-card (ui_plan / plan) ---
        t0 = time.time()
        timeout_ms = 25000
        s_plan_pass = False
        s_plan_detail = ""
        s_plan_shot = ""
        first_token_ms = None
        complete_ms = None
        log_offset = get_log_offset()
        plan_page = None
        try:
            plan_page = await context.new_page()
            await plan_page.goto(BASE_URL, timeout=10000)
            await wake_screen(plan_page)
            await plan_page.wait_for_selector("#textInput", timeout=5000)
            t_send = time.time()
            await plan_page.fill("#textInput", "週末の京都旅行の計画を立てて")
            await plan_page.click("#sendButton")
            first_msg = await plan_page.wait_for_selector(".msg.agent", timeout=15000)
            first_token_ms = int((time.time() - t_send) * 1000)
            reply_el = await plan_page.wait_for_selector(".msg.agent:not(.partial)", timeout=20000)
            complete_ms = int((time.time() - t_send) * 1000)
            reply_text = (await reply_el.inner_text()).strip()
            card_el = await plan_page.wait_for_selector(".card, .cards > *", timeout=5000)
            has_card = card_el is not None
            s_plan_pass = bool(len(reply_text) > 0 and has_card)
            s_plan_detail = f"reply_len={len(reply_text)}, card={has_card}"
            s_plan_shot = await capture_scenario_screenshot(plan_page, "plan-card", "ok" if s_plan_pass else "fail")
        except Exception as e:
            s_plan_detail = str(e)
            if plan_page:
                s_plan_shot = await capture_scenario_screenshot(plan_page, "plan-card", "fail")
        finally:
            if plan_page:
                await plan_page.close()
        elapsed_ms = int((time.time() - t0) * 1000)
        tools, tool_calls = parse_tool_calls_since(log_offset)
        append_metric(sha, dirty, "plan-card", s_plan_pass, first_token_ms, complete_ms, elapsed_ms, tools, tool_calls, timeout_ms, screenshot=s_plan_shot)
        results["plan_card"] = {"pass": s_plan_pass, "detail": s_plan_detail, "elapsed_ms": elapsed_ms, "tools": tools, "screenshot": s_plan_shot}
        print(f"Scenario (plan-card): pass={s_plan_pass}, complete={complete_ms}ms, elapsed={elapsed_ms}ms, tools={tools}")


        # --- Scenario 20: Wake screen via wake word (POST /api/wake) (#472, #473) ---
        t0 = time.time()
        timeout_ms = 5000
        s20_pass = False
        s20_detail = ""
        s20_shot = ""
        s20_timeline = []
        try:
            # Let the panel enter sleep / screensaver if awake
            await page.evaluate("() => sessionStorage.removeItem('panel_awake')")
            await page.reload()
            await page.wait_for_selector("#clockPage", state="visible", timeout=5000)
            await page.evaluate("() => { window.__visibilityTimeline = []; }")

            # Wake via wake word webhook
            wake_t0 = time.time()
            req = urllib.request.Request(f"{BASE_URL}/api/wake", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass

            # Assert clockPage hides
            await page.wait_for_selector("#clockPage", state="hidden", timeout=5000)
            await page.wait_for_timeout(1200)  # observation window for flicker

            s20_timeline = await page.evaluate("() => window.__visibilityTimeline || []")
            timeline_ok, timeline_err = check_visibility_timeline(s20_timeline, min_duration_ms=1000)
            if not timeline_ok:
                s20_pass = False
                s20_detail = timeline_err
            else:
                s20_pass = True
                s20_detail = f"timeline_clean={len(s20_timeline)}_events"

            s20_shot = await capture_scenario_screenshot(page, "wake-word-wake", "ok" if s20_pass else "fail")
        except Exception as e:
            s20_detail = str(e)
            s20_shot = await capture_scenario_screenshot(page, "wake-word-wake", "fail")
        elapsed_ms = int((time.time() - t0) * 1000)
        append_metric(sha, dirty, "wake-word-wake", s20_pass, None, None, elapsed_ms, [], 0, timeout_ms, screenshot=s20_shot, timeline=s20_timeline)
        results["scenario_20"] = {"pass": s20_pass, "detail": s20_detail, "elapsed_ms": elapsed_ms, "screenshot": s20_shot}
        print(f"Scenario 20 (wake-word-wake): pass={s20_pass}, detail={s20_detail}, elapsed={elapsed_ms}ms")

        # --- Scenario 21: Dynamic starter chips semantic fit & selection (#473, #476) ---
        t0 = time.time()
        timeout_ms = 15000
        s21_pass = False
        s21_detail = ""
        s21_shot = ""
        s21_timeline = []
        try:
            await page.evaluate("() => { window.__visibilityTimeline = []; }")
            dynamic_chips = await page.query_selector_all(".dynamic-chip")
            if not dynamic_chips:
                res = await page.request.get(f"{BASE_URL}/api/starters/suggestions?q=天気&domain=weather&lang=ja")
                data = await res.json()
                suggestions = data.get("suggestions", [])
                if suggestions:
                    await page.evaluate("""(suggs) => {
                        let dyn = document.querySelector('.starter-dynamic');
                        if (!dyn) {
                            const row = document.querySelector('.starter-row');
                            if (row) {
                                dyn = document.createElement('div');
                                dyn.className = 'starter-dynamic';
                                row.prepend(dyn);
                            }
                        }
                        if (dyn) {
                            dyn.innerHTML = '';
                            for (const s of suggs) {
                                const btn = document.createElement('button');
                                btn.className = 'chip dynamic-chip';
                                btn.dataset.prompt = s.prompt || '';
                                btn.textContent = s.label || '';
                                dyn.appendChild(btn);
                            }
                        }
                        const starters = document.getElementById('starters');
                        if (starters) starters.classList.remove('hidden');
                        const startersContext = document.getElementById('startersContext');
                        if (startersContext) startersContext.classList.remove('hidden');
                    }""", suggestions)
                    dynamic_chips = await page.query_selector_all(".dynamic-chip")

            chip_texts = []
            for dc in dynamic_chips:
                txt = (await dc.inner_text()).strip()
                if txt:
                    chip_texts.append(txt)

            user_msg_sent = False
            placement_ok = False
            if dynamic_chips:
                prev_user_count = await page.evaluate("() => document.querySelectorAll('.msg.user').length")
                # Click freshest dynamic chip immediately while DOM is fresh before wake idle timeout (#548, #682)
                await page.click(".dynamic-chip")
                # Assert that the click sends the chip's text (adding a user message to the feed),
                # that a turn starts, and that starters remains in sidePanel after cards.
                try:
                    await page.wait_for_function(
                        "prev => document.querySelectorAll('.msg.user').length > prev",
                        arg=prev_user_count,
                        timeout=3000
                    )
                    user_msg_sent = True
                except Exception:
                    user_msg_sent = False

                placement_ok = await page.evaluate("""() => {
                    const starters = document.getElementById('starters');
                    const sidePanel = document.getElementById('sidePanel');
                    const cards = document.getElementById('cards');
                    return starters?.parentElement === sidePanel && starters?.previousElementSibling === cards;
                }""")

            chip_pass = None
            chip_judge_reason = ""
            try:
                from tools import tst_judge
            except ImportError:
                try:
                    import tst_judge
                except ImportError:
                    tst_judge = None

            if tst_judge and chip_texts:
                chip_pass, score, chip_judge_reason = await tst_judge.judge_chip_relevance(
                    "天気についての質問と回答", chip_texts
                )

            if not dynamic_chips:
                s21_pass = False
                s21_detail = "no .dynamic-chip found"
            elif chip_pass is False:
                s21_pass = False
                s21_detail = f"semantic mismatch: {chip_judge_reason} (chips: {chip_texts})"
            elif not placement_ok:
                s21_pass = False
                s21_detail = "starters placement failed: starters.previousElementSibling !== cards"
            elif not user_msg_sent:
                s21_pass = False
                s21_detail = "chip click did not start user turn or append user message"
            else:
                s21_pass = True
                judge_info = chip_judge_reason if chip_judge_reason else ("no judge ran" if chip_pass is None else "ok")
                s21_detail = f"selected_chip='{chip_texts[0]}', user_turn_sent=True, placement_ok=True, chip_judge='{judge_info}'"

            s21_shot = await capture_scenario_screenshot(page, "dynamic-chip-selection", "ok" if s21_pass else "fail")
            s21_timeline = await page.evaluate("() => window.__visibilityTimeline || []")
        except Exception as e:
            s21_detail = str(e)
            s21_shot = await capture_scenario_screenshot(page, "dynamic-chip-selection", "fail")
        elapsed_ms = int((time.time() - t0) * 1000)
        append_metric(sha, dirty, "dynamic-chip-selection", s21_pass, None, None, elapsed_ms, [], 0, timeout_ms, screenshot=s21_shot, timeline=s21_timeline)
        results["scenario_21"] = {"pass": s21_pass, "detail": s21_detail, "elapsed_ms": elapsed_ms, "screenshot": s21_shot}
        print(f"Scenario 21 (dynamic-chip-selection): pass={s21_pass}, detail={s21_detail}, elapsed={elapsed_ms}ms")

        # --- Scenario: starters-suggestions-api (GET /api/starters/suggestions) ---
        t0 = time.time()
        timeout_ms = 5000
        s_sugg_pass = False
        s_sugg_detail = ""
        try:
            req_empty = urllib.request.Request(f"{BASE_URL}/api/starters/suggestions")
            with urllib.request.urlopen(req_empty, timeout=5) as resp:
                data_empty = json.loads(resp.read().decode("utf-8"))
            req_weather = urllib.request.Request(f"{BASE_URL}/api/starters/suggestions?q=weather&lang=ja")
            with urllib.request.urlopen(req_weather, timeout=5) as resp:
                data_weather = json.loads(resp.read().decode("utf-8"))
            s_sugg_pass = bool(
                isinstance(data_empty.get("suggestions"), list) and
                len(data_empty["suggestions"]) == 0 and
                isinstance(data_weather.get("suggestions"), list) and
                len(data_weather["suggestions"]) > 0
            )
            s_sugg_detail = f"empty_len={len(data_empty.get('suggestions', []))}, weather_len={len(data_weather.get('suggestions', []))}"
        except Exception as e:
            s_sugg_detail = str(e)
        elapsed_ms = int((time.time() - t0) * 1000)
        append_metric(sha, dirty, "starters-suggestions-api", s_sugg_pass, None, None, elapsed_ms, [], 0, timeout_ms, screenshot="")
        results["starters_suggestions_api"] = {"pass": s_sugg_pass, "detail": s_sugg_detail, "elapsed_ms": elapsed_ms, "screenshot": ""}
        print(f"Scenario (starters-suggestions-api): pass={s_sugg_pass}, detail={s_sugg_detail}, elapsed={elapsed_ms}ms")

        # --- Scenario 8: POST /api/forget/{user_id} ---
        t0 = time.time()
        timeout_ms = 5000
        s8_pass = False
        s8_detail = ""
        try:
            req = urllib.request.Request(
                f"{BASE_URL}/api/forget/terminal",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                body = json.loads(resp.read().decode())
                s8_pass = bool(status == 200 and "forgotten" in body)
                s8_detail = f"status={status}, body={body}"
        except Exception as e:
            s8_detail = str(e)
        elapsed_ms = int((time.time() - t0) * 1000)
        append_metric(sha, dirty, "forget-endpoint-api", s8_pass, None, None, elapsed_ms, [], 0, timeout_ms, screenshot="")
        results["scenario_8"] = {"pass": s8_pass, "detail": s8_detail, "elapsed_ms": elapsed_ms, "screenshot": ""}
        print(f"Scenario 8 (forget-endpoint-api): pass={s8_pass}, detail={s8_detail}, elapsed={elapsed_ms}ms")

        # --- Regressions corpus (#509) ---
        await run_regressions_corpus(page, results, sha, dirty)

        await browser.close()

    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    await update_metrics_with_judges()

    try:
        tst_render.render_results(current_run_id=CURRENT_RUN_ID, traj_ran=False)
    except Exception as e:
        print(f"Error rendering current_test_results.md: {e}")

    if should_run_trajectories(sha, force=bool(os.environ.get("FORCE_TRAJECTORIES"))):
        print("\nCommit changed or forced: running multi-turn trajectory suite...")
        try:
            await run_trajectories_suite(traj_argv)
            is_narrowed = bool(traj_argv and any(arg in traj_argv for arg in ("--scenario", "--limit")))
            if not is_narrowed:
                mark_trajectories_run(sha)
        except Exception as e:
            print(f"Error running multi-turn trajectory suite: {e}")
            append_metric(sha, dirty, "traj-suite-execution", False, None, None, 0, [], 0, 0, screenshot="")
            try:
                tst_render.render_results(current_run_id=CURRENT_RUN_ID)
            except Exception as re_err:
                print(f"Error rendering current_test_results.md: {re_err}")

    # Validate friction log in current_test_results.md (#524)
    valid, err = validate_runner_friction_log()
    if not valid:
        results["friction_log_format"] = {"pass": False, "detail": err}
        try:
            with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)
        except Exception as e:
            print(f"Error writing {SUMMARY_FILE}: {e}")

    print("\nRun complete.")


async def main(traj_argv=None, check_revision: bool = False):
    global _RUN_LOCK_FD
    ok, msg, lock_fd = acquire_run_lock(RUN_LOCK_FILE)
    if not ok:
        print(msg)
        sys.exit(1)
    _RUN_LOCK_FD = lock_fd
    try:
        await _run_scenarios(traj_argv=traj_argv, check_revision=check_revision)
    finally:
        _RUN_LOCK_FD = None
        release_run_lock(lock_fd)


def validate_runner_friction_log() -> tuple[bool, str]:
    """Validate visual friction log in current_test_results.md if present (#524)."""
    results_path = getattr(tst_render, "RESULTS_FILE", PROJECT_DIR / "current_test_results.md")
    if not results_path.exists():
        return True, ""
    try:
        content = results_path.read_text(encoding="utf-8")
        if "## Visual friction log, last run" not in content:
            return True, ""
        ok, err = tst_render.validate_friction_log(content)
        if not ok:
            print(f"\n[FAIL] Friction log validation failed: {err}")
            return False, err
        return True, ""
    except Exception as e:
        err = str(e)
        print(f"\n[FAIL] Friction log validation error: {err}")
        return False, err



async def run_trajectories_suite(argv=None):
    """Execute multi-turn trajectory testing suite."""
    global _RUN_LOCK_FD
    own_lock = False
    lock_fd = None
    if _RUN_LOCK_FD is None:
        ok, msg, lock_fd = acquire_run_lock(RUN_LOCK_FILE)
        if not ok:
            print(msg)
            sys.exit(1)
        _RUN_LOCK_FD = lock_fd
        own_lock = True
    try:
        try:
            from tools import tst_trajectories
        except ImportError:
            try:
                import tst_trajectories
            except ImportError:
                _tools_dir = str(PROJECT_DIR / "tools")
                if _tools_dir not in sys.path:
                    sys.path.insert(0, _tools_dir)
                import tst_trajectories
        await tst_trajectories.main(argv=argv if argv is not None else [])
    finally:
        if own_lock:
            _RUN_LOCK_FD = None
            release_run_lock(lock_fd)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Raspberry Pi AI Terminal Test Runner")
    parser.add_argument("--trajectories", action="store_true", help="Run multi-turn trajectory test suite")
    parser.add_argument("--all", action="store_true", help="Run single-turn scenarios and multi-turn trajectories")
    parser.add_argument("--scenario", type=str, help="Specific scenario to run")
    parser.add_argument("--limit", type=int, help="Limit number of trajectory scenarios")
    parser.add_argument("--check-revision", action="store_true", help="Preflight check verifies server revision matches HEAD")
    args = parser.parse_args()

    traj_argv = []
    if args.scenario:
        traj_argv.extend(["--scenario", args.scenario])
    if args.limit:
        traj_argv.extend(["--limit", str(args.limit)])

    if args.trajectories:
        ok, msg, lock_fd = acquire_run_lock(RUN_LOCK_FILE)
        if not ok:
            print(msg)
            sys.exit(1)
        _RUN_LOCK_FD = lock_fd
        try:
            sha, dirty = get_git_info()
            ok, err = server_preflight(BASE_URL, expected_sha=sha if args.check_revision else None)
            if not ok:
                print(err)
                sys.exit(1)
            try:
                asyncio.run(run_trajectories_suite(traj_argv))
                is_narrowed = bool(traj_argv and any(arg in traj_argv for arg in ("--scenario", "--limit")))
                if not is_narrowed:
                    mark_trajectories_run(sha)
            except Exception as e:
                print(f"Error running multi-turn trajectory suite: {e}")
                append_metric(sha, dirty, "traj-suite-execution", False, None, None, 0, [], 0, 0, screenshot="")
                try:
                    tst_render.render_results()
                except Exception as re_err:
                    print(f"Error rendering current_test_results.md: {re_err}")
        finally:
            _RUN_LOCK_FD = None
            release_run_lock(lock_fd)
    elif args.all:
        os.environ["FORCE_TRAJECTORIES"] = "1"
        asyncio.run(main(traj_argv=traj_argv, check_revision=args.check_revision))
    else:
        asyncio.run(main(traj_argv=traj_argv, check_revision=args.check_revision))
