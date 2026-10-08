#!/usr/bin/env python3
"""Multi-turn trajectory testing harness for Raspberry Pi AI Terminal.

Implements automated multi-turn scenarios that interact across multiple
conversational turns over WebSocket (http://127.0.0.1:8021) using text turns
or DOM interactions.

Evaluates end-to-end user problem solving across sequential turns:
- Context retention and chained reasoning across turns
- Tool trajectory execution (maps, route, weather, search, youtube, image, calc)
- Per-turn latency (first_token_ms, complete_ms) and total wall-clock elapsed_ms
- Automatic screenshot capture on turn completion / failures
- Metric persistence to /tmp/tst-trajectories.jsonl, tst_metrics.jsonl,
  and markdown reports via tst_render.py
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.request

from playwright.async_api import BrowserContext, Page, async_playwright

PROJECT_DIR = Path(__file__).resolve().parent.parent

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

BASE_URL = os.environ.get("AI_TERMINAL_BASE_URL", "http://127.0.0.1:8021")
SERVER_LOG = "/tmp/tst-server.log"
SCREENSHOT_DIR = "/tmp/tst-screenshots"
TRAJECTORIES_FILE = "/tmp/tst-trajectories.jsonl"
REPORT_FILE = "/tmp/tst-trajectories.md"
METRICS_FILE = Path("/tmp/tst-metrics.jsonl")
REPO_METRICS_FILE = PROJECT_DIR / "tst_metrics.jsonl"

os.makedirs(SCREENSHOT_DIR, exist_ok=True)

SCENARIOS: List[Dict[str, Any]] = [
    {
        "num": 1,
        "id": "traj-dining-directions",
        "title": "Local Dining & Directions",
        "expected_tools": ["maps_search", "maps_route"],
        "turns": [
            "鎌倉駅周辺の美味しいイタリアンレストランを探して",
            "そのお店の評判や看板メニューについて詳しく教えて",
            "鎌倉駅からそのお店までの徒歩での行き方を教えて",
        ],
    },
    {
        "num": 2,
        "id": "traj-news-video",
        "title": "News to Video Exploration",
        "expected_tools": ["web_search", "youtube_search"],
        "turns": [
            "日本の今日の最新ニュースを教えて",
            "その中で一番目のニュースについて、背景や詳細を教えて",
            "そのニュースに関連する解説動画を見せて",
        ],
    },
    {
        "num": 3,
        "id": "traj-trip-budget",
        "title": "Trip Planning & Expense Calculation",
        "expected_tools": ["web_search", "maps_search"],
        "turns": [
            "東京から京都への新幹線指定席の片道料金を調べて",
            "京都駅周辺の観光に便利なホテルを地図で探して",
            "新幹線の往復代約28,000円とホテル代14,000円を合わせた合計費用を計算して",
        ],
    },
    {
        "num": 4,
        "id": "traj-recipe-cooking",
        "title": "Recipe to Cooking Steps",
        "expected_tools": ["web_search", "youtube_search"],
        "turns": [
            "本格的なキーマカレーのレシピを教えて",
            "ガラムマサラがない場合、代わりに使える身近な調味料やスパイスはありますか？",
            "キーマカレーの作り方を動画でわかりやすく見せて",
        ],
    },
    {
        "num": 5,
        "id": "traj-weekend-outing",
        "title": "Weekend Outing Planning",
        "expected_tools": ["weather", "maps_route"],
        "turns": [
            "明日の鎌倉の天気を教えて",
            "その天気に合わせて、鎌倉のおすすめの散歩スポットや見どころを教えて",
            "鎌倉駅から鶴岡八幡宮への徒歩ルートと所要時間を教えて",
        ],
    },
    {
        "num": 6,
        "id": "traj-schedule-quiet-place",
        "title": "Schedule Coordination & Meeting Prep",
        "expected_tools": ["maps_search"],
        "turns": [
            "今日の午後は15時から17時まで2時間の空き時間があります。おすすめの過ごし方はありますか？",
            "落ち着いて読書や作業ができる静かな場所がいいです",
            "東京駅近くの静かなカフェを地図で探して",
        ],
    },
    {
        "num": 7,
        "id": "traj-landmark-cultural",
        "title": "Landmark & Cultural Exploration",
        "expected_tools": ["image_search", "maps_search"],
        "turns": [
            "金沢の兼六園の写真を見せて",
            "兼六園の見どころと、一番おすすめの観光シーズンを教えて",
            "兼六園の近くにある温泉旅館や宿を地図で探して",
        ],
    },
    {
        "num": 8,
        "id": "traj-financial-digest",
        "title": "Financial Market Digest & Conversion",
        "expected_tools": ["web_search"],
        "turns": [
            "今日の日経平均株価やドル円為替の最新マーケット情報を教えて",
            "最近のドル円相場の主な変動要因をわかりやすく説明して",
            "1ドル155円として、3200ドルを日本円に計算するといくらになりますか？",
        ],
    },
    {
        "num": 9,
        "id": "traj-appliance-troubleshooting",
        "title": "Household Appliance Troubleshooting",
        "expected_tools": ["web_search", "youtube_search"],
        "turns": [
            "電気ケトルの底についた白い水垢をクエン酸で落とす手順を教えて",
            "ケトルのクエン酸洗浄の手順を解説している動画を見せて",
            "クエン酸洗浄をするときの注意点と手入れの頻度を教えて",
        ],
    },
    {
        "num": 10,
        "id": "traj-multilingual-assistance",
        "title": "Multilingual Local Guide Assistance",
        "expected_tools": ["web_search", ["maps_search", "place_info"]],
        "turns": [
            "What are the rules for sorting plastic bottles and burnable trash in Tokyo?",
            "How can I politely ask my Japanese neighbor what day the recyclable trash is collected? Please give me the Japanese phrases with romaji.",
            "新宿区役所の場所を地図で探して",
        ],
    },
]


def get_git_info() -> Tuple[str, bool]:
    """Retrieve git HEAD sha and dirty status."""
    try:
        sha = subprocess.check_output(
            ["git", "-C", str(PROJECT_DIR), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        diff = subprocess.check_output(
            ["git", "-C", str(PROJECT_DIR), "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return sha, bool(diff)
    except Exception:
        return "unknown", False


def get_log_offset() -> int:
    """Return byte offset of the server log file."""
    if os.path.exists(SERVER_LOG):
        return os.path.getsize(SERVER_LOG)
    return 0


class ToolCallsCount(int):
    """Integer count of tool calls that also stores structured call and result records."""
    def __new__(cls, count: int, calls: Optional[List[str]] = None, results: Optional[List[Dict[str, Any]]] = None):
        inst = super().__new__(cls, count)
        inst.calls = calls if calls is not None else []
        inst.results = results if results is not None else []
        return inst


class ToolsList(list):
    """List of tool names that also preserves captured tool results and calls."""
    def __init__(self, items: List[str], results: Optional[List[Dict[str, Any]]] = None, calls: Optional[List[str]] = None):
        super().__init__(items)
        self.results = results if results is not None else []
        self.calls = calls if calls is not None else []


class ToolCallsTuple(tuple):
    """Tuple (tools, tool_calls) that preserves attributes for tool results."""
    def __new__(cls, tools: Any, tool_calls: Any, results: Optional[List[Dict[str, Any]]] = None):
        inst = super().__new__(cls, (tools, tool_calls))
        inst.tools = tools
        inst.tool_calls = tool_calls
        inst.results = results if results is not None else []
        inst.tool_results = results if results is not None else []
        return inst


def _parse_tool_shape(raw: str) -> Any:
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


def parse_tool_calls_since(
    log_offset: int, include_results: bool = False
) -> Tuple[List[str], int] | Tuple[List[str], int, List[Dict[str, Any]]]:
    """Extract tool calls and results logged since log_offset."""
    tools: List[str] = []
    tool_results: List[Dict[str, Any]] = []
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


def parse_tool_results_since(log_offset: int) -> List[Dict[str, Any]]:
    """Extract tool results logged since log_offset."""
    _, _, results = parse_tool_calls_since(log_offset, include_results=True)
    return results


def reset_terminal_session(user_id: str = "terminal") -> bool:
    """Reset terminal conversation history using the forget endpoint."""
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/api/forget/{user_id}",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"Warning: Failed to reset session via forget API: {e}")
        return False


async def capture_scenario_screenshot(
    page: Page, name: str, status: str = "ok"
) -> str:
    """Capture a screenshot into /tmp/tst-screenshots."""
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{name}_{status}_{ts}.png"
    filepath = os.path.join(SCREENSHOT_DIR, filename)
    try:
        if hasattr(page, "is_closed") and page.is_closed():
            return ""
        await page.screenshot(path=filepath, full_page=False)
        return filepath
    except Exception as e:
        print(f"Warning: Screenshot capture failed for {name}: {e}")
        return ""


async def wake_screen(page: Page) -> None:
    """Ensure clock screensaver is dismissed and feed is visible."""
    try:
        clock = await page.query_selector("#clockPage")
        if clock and await clock.is_visible():
            await clock.click()
            await page.wait_for_selector("#clockPage", state="hidden", timeout=5000)
    except Exception:
        pass


async def evaluate_context_retention(
    turns_data: List[Dict[str, Any]], scenario_def: Dict[str, Any]
) -> Tuple[Optional[bool], str]:
    """Evaluate whether context was retained across turns via judge or phrase fallback (#473)."""
    if len(turns_data) < 2:
        return True, "single turn"
    if not all(t.get("pass") for t in turns_data):
        return False, "turn failed"

    try:
        from tools import tst_judge
    except ImportError:
        try:
            import tst_judge
        except ImportError:
            tst_judge = None

    retained: Optional[bool] = None
    reason: str = "judge unavailable"
    if tst_judge:
        intent = scenario_def.get("intent", scenario_def.get("id", ""))
        retained, reason = await tst_judge.judge_context_retention(intent, turns_data)
        if retained is False:
            return False, reason

    lost_context_phrases = [
        "何の件でしょうか",
        "何を指しているかわかりません",
        "どのことでしょうか",
        "具体的な場所を指定してください",
        "どちらの話でしょうか",
    ]
    for t in turns_data[1:]:
        resp = t.get("response", "")
        for phrase in lost_context_phrases:
            if phrase in resp:
                return False, f"context lost phrase matched: '{phrase}'"
    return retained, reason


async def execute_turn(
    page: Page,
    turn_num: int,
    prompt: str,
    timeout_ms: int = 40000,
    scenario_id: str = "",
    sha: str = "",
) -> Dict[str, Any]:
    """Execute a single conversational turn in the browser page."""
    turn_t0 = time.time()
    turn_log_offset = get_log_offset()
    prior_agent_count = len(await page.query_selector_all(".msg.agent"))

    await wake_screen(page)
    await page.wait_for_selector("#textInput", timeout=5000)
    await page.fill("#textInput", prompt)
    await page.wait_for_selector("#sendButton:not([disabled])", timeout=5000)
    await page.click("#sendButton")

    first_token_ms: Optional[int] = None
    complete_ms: Optional[int] = None
    response_text = ""

    poll_start = time.time()
    deadline = poll_start + (timeout_ms / 1000.0)

    while time.time() < deadline:
        await page.wait_for_timeout(300)

        if first_token_ms is None:
            cur_agents = await page.query_selector_all(".msg.agent")
            if len(cur_agents) > prior_agent_count:
                first_token_ms = int((time.time() - turn_t0) * 1000)

        cur_sealed = await page.query_selector_all(".msg.agent:not(.partial)")
        if len(cur_sealed) > prior_agent_count:
            working = await page.query_selector(".working")
            is_working = await working.is_visible() if working else False
            if not is_working:
                complete_ms = int((time.time() - turn_t0) * 1000)
                last_bubble = cur_sealed[-1]
                response_text = (await last_bubble.inner_text()).strip()
                break

    if complete_ms is None:
        complete_ms = int((time.time() - turn_t0) * 1000)
        cur_agents = await page.query_selector_all(".msg.agent")
        if len(cur_agents) > prior_agent_count:
            response_text = (await cur_agents[-1].inner_text()).strip()

    turn_tools, turn_tool_calls = parse_tool_calls_since(turn_log_offset)
    elapsed_ms = int((time.time() - turn_t0) * 1000)
    turn_pass = bool(len(response_text) > 0)

    if not turn_pass and scenario_id:
        fail_name = f"{scenario_id}_turn{turn_num}_{sha}"
        await capture_scenario_screenshot(page, fail_name, "fail")

    return {
        "turn": turn_num,
        "prompt": prompt,
        "response": response_text[:160] + ("..." if len(response_text) > 160 else ""),
        "first_token_ms": first_token_ms,
        "complete_ms": complete_ms,
        "elapsed_ms": elapsed_ms,
        "tools": turn_tools,
        "tool_calls": turn_tool_calls,
        "pass": turn_pass,
    }


def append_trajectory_metric(
    sha: str,
    dirty: bool,
    scenario_id: str,
    passed: bool,
    first_token_ms: Optional[int],
    complete_ms: Optional[int],
    elapsed_ms: int,
    tools: List[str],
    tool_calls: int,
    turns_cnt: int,
    context_retained: Optional[bool],
    run_id: str,
    screenshot: str = "",
    timeout_ms: int = 120000,
    expected_tools: Optional[List[str]] = None,
    context_reason: str = "",
) -> Dict[str, Any]:
    """Append aggregated trajectory metric row to metrics stores."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {
        "ts": ts,
        "run_id": run_id,
        "sha": sha,
        "dirty": dirty,
        "scenario": scenario_id,
        "pass": passed,
        "first_token_ms": first_token_ms,
        "complete_ms": complete_ms,
        "elapsed_ms": elapsed_ms,
        "tools": tools,
        "tool_calls": tool_calls,
        "expected_tools": expected_tools or [],
        "turns": turns_cnt,
        "context_retained": context_retained,
        "context_reason": context_reason,
        "timeout_ms": timeout_ms,
        "screenshot": screenshot or "",
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    try:
        with open(METRICS_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"Warning: Failed to write to {METRICS_FILE}: {e}")

    try:
        with open(REPO_METRICS_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"Warning: Failed to write to {REPO_METRICS_FILE}: {e}")

    return entry


def check_expected_tools(expected_tools: List[Any], actual_tools: List[str]) -> bool:
    """Check if actual_tools satisfies expected_tools specification.

    Each entry in expected_tools can be a single tool string or a collection
    of alternatives (list, tuple, set) where any match satisfies the condition.
    """
    for exp in expected_tools:
        if isinstance(exp, (list, tuple, set)):
            if not any(t in actual_tools for t in exp):
                return False
        elif exp not in actual_tools:
            return False
    return True


async def run_scenario(
    context: BrowserContext,
    scenario_def: Dict[str, Any],
    run_id: str,
    sha: str,
    dirty: bool,
) -> Dict[str, Any]:
    """Execute all turns of a multi-turn trajectory scenario."""
    scenario_id = scenario_def["id"]
    scenario_num = scenario_def["num"]
    title = scenario_def["title"]
    print(f"\n--- Running Trajectory #{scenario_num}: {title} ({scenario_id}) ---")

    reset_terminal_session()
    scenario_t0 = time.time()
    scenario_log_offset = get_log_offset()

    page = await context.new_page()
    turns_data: List[Dict[str, Any]] = []
    scenario_pass = True
    screenshot_path = ""
    expected_tools = scenario_def.get("expected_tools", [])

    try:
        await page.goto(BASE_URL, timeout=15000)
        await page.wait_for_timeout(1000)
        await wake_screen(page)

        for idx, prompt in enumerate(scenario_def["turns"], start=1):
            print(f"  [Turn {idx}] User: {prompt}")
            turn_res = await execute_turn(
                page, idx, prompt, scenario_id=scenario_id, sha=sha
            )
            turns_data.append(turn_res)
            print(
                f"  [Turn {idx}] Reply ({turn_res['complete_ms']}ms): "
                f"{turn_res['response']} | Tools: {turn_res['tools']}"
            )
            if not turn_res["pass"]:
                scenario_pass = False

            await page.wait_for_timeout(1000)

        all_tools, all_tool_calls = parse_tool_calls_since(scenario_log_offset)
        if expected_tools and not check_expected_tools(expected_tools, all_tools):
            print(f"  Expected tools {expected_tools} not satisfied by {all_tools}")
            scenario_pass = False

        for res in getattr(all_tool_calls, "results", []):
            if res.get("tool") == "place_info":
                shape = res.get("shape", {})
                if isinstance(shape, dict) and shape.get("images") == 0 and shape.get("sources") == 0:
                    print(f"  place_info returned empty images and sources: {shape}")
                    scenario_pass = False

        scenario_elapsed_ms = int((time.time() - scenario_t0) * 1000)
        screenshot_path = await capture_scenario_screenshot(
            page, scenario_id, "ok" if scenario_pass else "fail"
        )

    except Exception as e:
        print(f"  Error executing {scenario_id}: {e}")
        scenario_pass = False
        scenario_elapsed_ms = int((time.time() - scenario_t0) * 1000)
        all_tools, all_tool_calls = parse_tool_calls_since(scenario_log_offset)
        screenshot_path = await capture_scenario_screenshot(page, scenario_id, "fail")

    finally:
        await page.close()

    context_retained, context_reason = await evaluate_context_retention(turns_data, scenario_def)
    first_token = turns_data[0]["first_token_ms"] if turns_data else None
    complete_sum = sum(
        t["complete_ms"] for t in turns_data if t["complete_ms"] is not None
    )

    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": run_id,
        "sha": sha,
        "dirty": dirty,
        "scenario": scenario_id,
        "scenario_num": scenario_num,
        "title": title,
        "pass": scenario_pass,
        "total_turns": len(turns_data),
        "context_retained": context_retained,
        "context_reason": context_reason,
        "expected_tools": expected_tools,
        "first_token_ms": first_token,
        "complete_ms": complete_sum,
        "elapsed_ms": scenario_elapsed_ms,
        "tools": all_tools,
        "tool_calls": all_tool_calls,
        "screenshot": screenshot_path,
        "turns": turns_data,
    }

    with open(TRAJECTORIES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    append_trajectory_metric(
        sha=sha,
        dirty=dirty,
        scenario_id=scenario_id,
        passed=scenario_pass,
        first_token_ms=first_token,
        complete_ms=complete_sum,
        elapsed_ms=scenario_elapsed_ms,
        tools=all_tools,
        tool_calls=all_tool_calls,
        turns_cnt=len(turns_data),
        context_retained=context_retained,
        context_reason=context_reason,
        run_id=run_id,
        screenshot=screenshot_path,
        expected_tools=expected_tools,
    )

    status_str = "PASS" if scenario_pass else "FAIL"
    print(
        f"Trajectory #{scenario_num} [{status_str}]: elapsed={scenario_elapsed_ms}ms, "
        f"context_retained={context_retained} ({context_reason}), tools={all_tools}, "
        f"screenshot={os.path.basename(screenshot_path)}"
    )

    return record


def render_report() -> None:
    """Generate markdown summary report in /tmp/tst-trajectories.md."""
    if not os.path.exists(TRAJECTORIES_FILE):
        return

    records: List[Dict[str, Any]] = []
    with open(TRAJECTORIES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass

    if not records:
        return

    latest_run_id = records[-1].get("run_id", "")
    latest_run = [r for r in records if r.get("run_id") == latest_run_id]
    if not latest_run:
        latest_run = records

    sha = latest_run[0].get("sha", "unknown")
    dirty = latest_run[0].get("dirty", False)
    ts = latest_run[0].get("ts", "")
    total_scenarios = len(latest_run)
    passed_scenarios = sum(1 for r in latest_run if r.get("pass"))

    lines = [
        f"# Multi-Turn Trajectory Test Results: `{sha}`{' (dirty)' if dirty else ''}",
        "",
        f"**Run ID**: `{latest_run_id}` | **Timestamp**: {ts} | "
        f"**Pass Rate**: {passed_scenarios}/{total_scenarios} "
        f"({passed_scenarios/total_scenarios*100:.1f}%)",
        "",
        "## Trajectory Scenarios Summary",
        "",
        "| # | Scenario | Turns | Context | Tool Trajectory | 1st Token (ms) | Total Elapsed (s) | Result | Screenshot |",
        "|---|---|:---:|:---:|---|:---:|:---:|:---:|---|",
    ]

    for r in latest_run:
        num = r.get("scenario_num", "")
        title = r.get("title", r.get("scenario", ""))
        turns_cnt = r.get("total_turns", 0)
        ctx = "ok" if r.get("context_retained") is True else ("skipped" if r.get("context_retained") is None else "lost")
        tools = ", ".join(r.get("tools", [])) or "*(none)*"
        first_token = f"{r.get('first_token_ms', 0)}ms" if r.get('first_token_ms') else "-"
        elapsed_sec = f"{r.get('elapsed_ms', 0) / 1000:.1f}s"
        status = "✅ PASS" if r.get("pass") else "❌ FAIL"
        shot = os.path.basename(r.get("screenshot", ""))
        lines.append(
            f"| {num} | **{title}** | {turns_cnt} | {ctx} | `{tools}` | "
            f"{first_token} | {elapsed_sec} | {status} | `{shot}` |"
        )

    lines.extend([
        "",
        "## Multi-Turn Detailed Trajectories",
        "",
    ])

    for r in latest_run:
        num = r.get("scenario_num", "")
        title = r.get("title", "")
        scen_id = r.get("scenario", "")
        status = "PASS" if r.get("pass") else "FAIL"
        ctx = "ok" if r.get("context_retained") is True else ("skipped" if r.get("context_retained") is None else "lost")
        lines.append(f"### Scenario {num}: {title} (`{scen_id}`) - {status} (context: {ctx})")
        lines.append(f"- **Tools called**: `{r.get('tools', [])}` (count: {r.get('tool_calls', 0)})")
        lines.append(f"- **Total Elapsed**: {r.get('elapsed_ms', 0)}ms")
        lines.append("")
        lines.append("| Turn | Prompt | Response Snippet | Latency (1st / Complete) | Tools |")
        lines.append("|:---:|---|---|:---:|---|")
        for t in r.get("turns", []):
            t_num = t.get("turn")
            t_prompt = t.get("prompt", "").replace("|", "\\|")
            t_resp = t.get("response", "").replace("\n", " ").replace("|", "\\|")
            lat = f"{t.get('first_token_ms') or '-'}ms / {t.get('complete_ms') or '-'}ms"
            t_tools = ", ".join(t.get("tools", [])) or "-"
            lines.append(f"| {t_num} | {t_prompt} | {t_resp} | {lat} | `{t_tools}` |")
        lines.append("")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nRendered report to {REPORT_FILE}")


async def main(argv: Optional[List[str]] = None) -> None:
    global BASE_URL
    parser = argparse.ArgumentParser(description="Multi-turn trajectory test harness")
    parser.add_argument("--scenario", type=str, help="Specific scenario number or id to run")
    parser.add_argument("--limit", type=int, help="Limit to first N scenarios")
    parser.add_argument("--report-only", action="store_true", help="Only render markdown report")
    parser.add_argument("--base-url", type=str, default=BASE_URL, help="Base URL of test server")
    parser.add_argument("--trajectories", action="store_true", help="Accepted for runner compatibility")
    parser.add_argument("--all", action="store_true", help="Accepted for runner compatibility")
    args = parser.parse_args(argv)

    BASE_URL = args.base_url

    if args.report_only:
        render_report()
        try:
            tst_render.render_results()
        except Exception as e:
            print(f"Warning: Failed to render test dashboard: {e}")
        return

    sha, dirty = get_git_info()
    run_id = f"run_traj_{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    to_run = SCENARIOS
    if args.scenario:
        scen_str = args.scenario.strip()
        to_run = [
            s for s in SCENARIOS
            if str(s["num"]) == scen_str or s["id"] == scen_str or scen_str in s["id"]
        ]
        if not to_run:
            print(f"Error: No scenario matching '{args.scenario}' found.")
            sys.exit(1)
    elif args.limit:
        to_run = SCENARIOS[: args.limit]

    print(f"Starting Multi-Turn Trajectory Run `{run_id}` for {len(to_run)} scenarios...")
    print(f"Commit SHA: {sha} (dirty: {dirty}) | Server: {BASE_URL}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1024, "height": 600},
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )

        for scen in to_run:
            await run_scenario(context, scen, run_id, sha, dirty)

        await browser.close()

    render_report()
    try:
        tst_render.render_results(current_run_id=run_id, traj_ran=True)
    except Exception as e:
        print(f"Warning: Failed to render test dashboard: {e}")

    print("\nAll requested trajectory scenarios finished.")


if __name__ == "__main__":
    asyncio.run(main())
