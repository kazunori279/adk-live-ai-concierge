# Browser-use spike

Throwaway scripts behind the *Browser use* section of the main README. Nothing
here is imported by the terminal; keep it that way or move the code up into
`src/`.

| script | runs on | what it does |
|---|---|---|
| `capture.py` | Pi | screenshots five real pages, plus their aria snapshot and `innerText`, and times each |
| `measure.py` | anything with ADC | counts tokens and times a round trip for each capture |
| `load.py` | Pi | second headless Chromium under a screenshot loop, sampling memory, load and SoC temperature |
| `demo.py` | Pi | the agent loop: screenshot + interactive-element list → Gemini → click/type, until it can answer |

```bash
uv sync
uv run python capture.py          # writes captures/
uv run python load.py             # run a kiosk Chromium first, and an arecord alongside
GOOGLE_CLOUD_PROJECT=... uv run python measure.py
```

`demo.py` needs Vertex credentials, and the Pi has them:

```bash
ssh pi2 "cd ~/browser-spike && GOOGLE_CLOUD_PROJECT=... uv run python demo.py"
```

The Pi used to have no ADC, and this file told you to mint an hour-long token on
the Mac and pass it in as `GOOGLE_CLOUD_ACCESS_TOKEN`. That is no longer
necessary — the Mac's ADC refresh token was copied to the Pi, so the Pi refreshes
for itself.

Set the variable anyway and `demo.py` uses it, for a machine with no ADC at all.
It is a branch and not a fallback: passing `credentials=` shuts off the SDK's
own lookup, so a token that has expired fails as an expired token rather than
quietly reverting to ADC.
