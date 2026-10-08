"""One real task, driven end to end on the Pi.

The loop the spike is testing: screenshot the page for perception, hand the model
a numbered list of the interactive elements for targeting, do what it says,
repeat. Screenshot-only targeting was tried first and failed the same way every
time — the model reads a placeholder off the pixels, Playwright cannot match it,
and the loop spins. Indices come from the live DOM, so they always resolve.

Runs on the Pi, off the Pi's own ADC. Set `GOOGLE_CLOUD_ACCESS_TOKEN` and it
uses that instead, which is how this was run before the Pi had credentials of
its own. See the README here for what changed.
"""
import json
import os
import sys
import time

from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from playwright.sync_api import sync_playwright

CHROME = "/usr/bin/chromium"
MODEL = "gemini-2.5-flash"
MAX_STEPS = 10
TASK = sys.argv[1] if len(sys.argv) > 1 else (
    "Starting from https://www.jorudan.co.jp/ , search for a train route from "
    "恵比寿 to 金沢 and report the first result's total travel time."
)

SYSTEM = """You drive a web browser for a voice assistant on a kitchen terminal.
Each turn you get a screenshot of the page and a numbered list of the elements
you can act on. Reply with one JSON object:
  {"act": "goto",  "url": "..."}
  {"act": "click", "idx": <number from the list>}
  {"act": "type",  "idx": <number>, "text": "..."}
  {"act": "press", "key": "Enter"}
  {"act": "answer", "text": "<the answer to the task>"}
Only use an index that appears in the list. If an action failed last turn, do
not repeat it — pick a different element. Answer as soon as the screenshot
shows what was asked."""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "act": {"type": "STRING",
                "enum": ["goto", "click", "type", "press", "answer"]},
        "url": {"type": "STRING"},
        "idx": {"type": "INTEGER"},
        "key": {"type": "STRING"},
        "text": {"type": "STRING"},
    },
    "required": ["act"],
}

# Tag every visible interactive element and report role, name and current value.
# Anything off-screen or zero-sized is skipped: the model is looking at a
# screenshot, so offering it elements it cannot see only invites bad picks.
TAG_JS = """(limit) => {
  const sel = 'a[href],button,input,select,textarea,[role=button],[role=link],[onclick]';
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
    if (getComputedStyle(el).visibility === 'hidden') continue;
    el.setAttribute('data-agent-idx', String(i));
    const name = (el.getAttribute('aria-label') || el.placeholder ||
                  el.value || el.innerText || el.title || '').trim().slice(0, 60);
    out.push(`${i}. <${el.tagName.toLowerCase()}> ${name.replace(/\\s+/g, ' ')}`);
    if (++i >= limit) break;
  }
  return out.join('\\n');
}"""

# ADC when there is any, which is what the Pi has now. Passing `credentials=`
# at all shuts the SDK's own lookup off, and a bare `Credentials(token=...)`
# carries no refresh token, so the explicit branch has to stay a branch: it is
# the old route, for a machine with no ADC and an hour-long token minted
# somewhere else.
_token = os.getenv("GOOGLE_CLOUD_ACCESS_TOKEN")
client = genai.Client(
    vertexai=True,
    project=os.environ["GOOGLE_CLOUD_PROJECT"],
    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-east1"),
    credentials=Credentials(token=_token) if _token else None,
)


def decide(shot, elems, history):
    t = time.time()
    seen = [f"Task: {TASK}\nDone so far: {history or 'nothing'}"]
    if shot:
        seen.append(types.Part.from_bytes(data=shot, mime_type="image/png"))
        seen.append(f"Elements you can act on:\n{elems}")
    else:
        seen.append("The browser is on a blank page. Navigate somewhere first.")
    r = client.models.generate_content(
        model=MODEL,
        contents=seen,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            response_mime_type="application/json",
            response_schema=SCHEMA,
        ),
    )
    usage = r.usage_metadata
    return (json.loads(r.text), round(time.time() - t, 2),
            usage.prompt_token_count, usage.candidates_token_count)


def act(page, step):
    kind = step["act"]
    if kind == "goto":
        page.goto(step["url"], wait_until="domcontentloaded", timeout=30000)
    elif kind == "press":
        page.keyboard.press(step.get("key", "Enter"))
    else:
        target = page.locator(f'[data-agent-idx="{step["idx"]}"]')
        if kind == "click":
            target.click(timeout=8000)
        else:
            target.fill(step["text"], timeout=8000)
    page.wait_for_timeout(2000)


def main():
    history = []
    total_in = total_out = 0
    t0 = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME, headless=True,
            args=["--password-store=basic"],
        )
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        for i in range(MAX_STEPS):
            # about:blank has no compositor surface on this Chromium build, so
            # the opening turn is text-only and only ever asks for a goto.
            blank = page.url == "about:blank"
            ts = time.time()
            shot = None if blank else page.screenshot()
            elems = "" if blank else page.evaluate(TAG_JS, 60)
            see_s = round(time.time() - ts, 2)
            step, model_s, tin, tout = decide(shot, elems, " then ".join(history))
            total_in += tin
            total_out += tout
            print(json.dumps({
                "step": i + 1, "see_s": see_s, "model_s": model_s,
                "elem_chars": len(elems), "in_tok": tin, "out_tok": tout,
                "act": step,
            }, ensure_ascii=False), flush=True)
            if step["act"] == "answer":
                break
            ta = time.time()
            try:
                act(page, step)
                history.append(json.dumps(step, ensure_ascii=False))
            except Exception as err:
                history.append(f"{json.dumps(step, ensure_ascii=False)} FAILED")
                print(f"  ! {str(err)[:90]}", flush=True)
            print(f"  act_s={round(time.time() - ta, 2)} url={page.url[:70]}",
                  flush=True)
        browser.close()
    print(json.dumps({"wall_s": round(time.time() - t0, 1),
                      "in_tok": total_in, "out_tok": total_out}), flush=True)


main()
