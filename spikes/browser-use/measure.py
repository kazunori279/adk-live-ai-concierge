"""Token cost and round-trip latency per page representation.

Runs on the Mac, not the Pi, for no better reason than that it was written
there. Token counts are a property of the payload, so they carry over
unchanged; the latency numbers are a Mac-to-Vertex round trip and exclude
whatever the Pi's own network leg adds.
"""

import json
import os
import pathlib
import time

from google import genai
from google.genai import types

CAP = pathlib.Path(os.environ.get("CAPTURES", "captures"))
MODEL = "gemini-2.5-flash"
ASK = (
    "You are driving a web browser for a voice assistant. Here is the current "
    "page. Say in one short sentence what you would click or type next to find "
    "the page's main article, and nothing else."
)

client = genai.Client(
    vertexai=True,
    project=os.environ["GOOGLE_CLOUD_PROJECT"],
    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-east1"),
)


def count(parts):
    return client.models.count_tokens(model=MODEL, contents=parts).total_tokens


def ask(parts):
    t = time.time()
    r = client.models.generate_content(
        model=MODEL,
        contents=parts,
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            max_output_tokens=60,
        ),
    )
    return round(time.time() - t, 2), (r.text or "").strip().replace("\n", " ")[:70]


def main():
    rows = []
    meta = {r["name"]: r for r in json.loads((CAP / "capture.json").read_text())}
    for name in ["example", "github-login", "hn", "wikipedia"]:
        png = (CAP / f"{name}.png").read_bytes()
        reps = {
            "screenshot": [
                ASK,
                types.Part.from_bytes(data=png, mime_type="image/png"),
            ],
            "aria": [ASK, (CAP / f"{name}.aria.txt").read_text()],
            "text": [ASK, (CAP / f"{name}.text.txt").read_text()],
        }
        for rep, parts in reps.items():
            tok = count(parts)
            secs, reply = ask(parts)
            row = {
                "page": name,
                "rep": rep,
                "tokens": tok,
                "model_s": secs,
                "capture_s": meta[name][f"{'shot' if rep == 'screenshot' else rep}_s"],
                "reply": reply,
            }
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    pathlib.Path("measure.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False)
    )


main()
