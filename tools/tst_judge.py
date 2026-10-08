"""LLM and vision judges for tester scenarios and trajectories (#473).

Evaluates:
1. Dynamic starter chips and contextual chip relevance against conversation turns.
2. Visual friction and interface defects from captured screenshots.
3. Multi-turn conversational context retention.

Uses gemini-3.8-flash with thinking_level=HIGH, executing concurrently after
scenarios complete to avoid inline latency degradation.
"""

import asyncio
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

MODEL_NAME = os.getenv("BROWSE_MODEL", "gemini-3.8-flash")


def get_client():
    if genai is None:
        return None
    try:
        from ai_terminal.live.tools._client import global_client
        return global_client()
    except Exception:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return None
        return genai.Client(api_key=api_key)


def get_thinking_config():
    if types is None:
        return None
    return types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.HIGH
        ),
        temperature=0.2,
    )


async def judge_chip_relevance(conversation: str, chip_labels: List[str]) -> Tuple[Optional[bool], float, str]:
    """Judge whether suggested chip labels are contextually relevant to the conversation."""
    if not chip_labels:
        return None, 0.0, "no chips to judge"

    client = get_client()
    if not client:
        # Fallback offline check: flag obvious autocomplete noise / URL / nonsense
        for label in chip_labels:
            if re.search(r"https?://|www\.", label):
                return False, 0.0, f"unprocessed URL in chip: {label}"
        return None, 0.0, "offline / judge skipped"

    prompt = f"""You are an expert UX evaluator judging conversation follow-up chips.
Conversation:
{conversation}

Suggested chips offered to user:
{', '.join(chip_labels)}

Evaluate whether these chips make sense as natural next actions or queries in this context.
Reply with a JSON object:
{{
  "score": <float between 0.0 and 1.0>,
  "pass": <boolean, true if score >= 0.6>,
  "reason": "<one sentence explanation>"
}}
"""
    try:
        config = get_thinking_config()
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt,
            config=config,
        )
        text = resp.text or ""
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            import json
            data = json.loads(m.group(0))
            score = float(data.get("score", 0.0))
            passed = bool(data.get("pass", score >= 0.6))
            reason = str(data.get("reason", ""))
            return passed, score, reason
        return None, 0.0, f"parse failure: {text.strip()}"
    except Exception as e:
        return None, 0.0, f"judge error: {e}"


async def judge_screenshot_friction(screenshot_path: str, scenario_intent: str) -> Optional[str]:
    """Read a scenario's captured screenshot and evaluate visual friction."""
    if not screenshot_path or not os.path.exists(screenshot_path):
        return ""

    client = get_client()
    if not client:
        return "friction check skipped (no client / offline)"

    try:
        img_bytes = Path(screenshot_path).read_bytes()
        prompt = f"""You are evaluating the visual UI friction of a Raspberry Pi smart terminal screen (1024x600).
Scenario intent: {scenario_intent}

Look at the screenshot and report:
- Any visual friction, overlapping elements, truncated text, misplaced buttons, or jarring visual artifacts.
- The right panel (.side-panel) is a vertically scrollable container. Content extending past the bottom edge of the viewport is expected fold behavior, NOT truncation or clipping. Only report truncation or clipping if content is clipped inside a non-scrollable box or cannot be scrolled into view.
- Keep the response to 1-2 concise sentences. If the screen looks completely clean and appropriate, reply 'none'.
"""
        config = get_thinking_config()
        part = types.Part.from_bytes(data=img_bytes, mime_type="image/png")
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL_NAME,
            contents=[part, prompt],
            config=config,
        )
        text = (resp.text or "").strip()
        if text.lower() == "none" or "no friction" in text.lower():
            return "none"
        return text
    except Exception as e:
        return None


async def judge_context_retention(intent: str, turns: List[Dict[str, Any]]) -> Tuple[Optional[bool], str]:
    """Judge whether conversational context was retained across multi-turn exchanges."""
    if len(turns) < 2:
        return True, "single turn"

    client = get_client()
    if not client:
        # Fallback to deterministic phrase blocklist
        lost_phrases = [
            "何の件でしょうか",
            "何を指しているかわかりません",
            "どのことでしょうか",
            "具体的な場所を指定してください",
            "どちらの話でしょうか",
        ]
        for t in turns[1:]:
            resp = t.get("response", "")
            for p in lost_phrases:
                if p in resp:
                    return False, f"context lost phrase matched: '{p}'"
        return None, "offline / judge skipped"

    conv_history = []
    for i, t in enumerate(turns):
        conv_history.append(f"Turn {i+1} User: {t.get('prompt', '')}")
        conv_history.append(f"Turn {i+1} Agent: {t.get('response', '')}")

    prompt = f"""Scenario intent: {intent}
Conversation transcript:
{chr(10).join(conv_history)}

Did the agent retain multi-turn conversational context, or did it forget previous context / ask for clarification that was already provided?
Reply with JSON:
{{
  "context_retained": <boolean>,
  "reason": "<brief explanation>"
}}
"""
    try:
        config = get_thinking_config()
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt,
            config=config,
        )
        text = resp.text or ""
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            import json
            data = json.loads(m.group(0))
            if "context_retained" in data:
                return bool(data["context_retained"]), str(data.get("reason", ""))
            return None, f"parse failure: missing context_retained in {data}"
        return None, f"parse failure: {text.strip()}"
    except Exception as e:
        return None, f"judge error: {e}"


async def run_post_run_judges_concurrent(metrics_entries: List[Dict[str, Any]]) -> None:
    """Concurrently evaluate friction and judges on completed scenario metric rows.

    Executes after all browser scenarios have finished so single-turn sweep
    latency is not degraded.
    """
    tasks = []

    async def _evaluate_entry(entry: Dict[str, Any]):
        shot = entry.get("screenshot")
        scenario = entry.get("scenario", "")
        if shot and os.path.exists(shot):
            res = await judge_screenshot_friction(shot, f"Scenario {scenario}")
            if res is None:
                entry["friction"] = None
                entry["friction_error"] = "judge error (unjudged)"
            else:
                entry["friction"] = res

    for entry in metrics_entries:
        tasks.append(_evaluate_entry(entry))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
