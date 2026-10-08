"""Capture each page three ways and time it, on the Pi, at the kiosk viewport.

Screenshot, Playwright aria snapshot, and body innerText. The point is what each
one costs to produce and how big it is; token counts happen off-device where the
credentials are.
"""
import json
import pathlib
import time

from playwright.sync_api import sync_playwright

CHROME = "/usr/bin/chromium"
OUT = pathlib.Path("captures")
PAGES = {
    "example": "https://example.com",
    "wikipedia": "https://en.wikipedia.org/wiki/Raspberry_Pi",
    "github-login": "https://github.com/login",
    "hn": "https://news.ycombinator.com",
    "weather": "https://www.timeanddate.com/weather/japan/tokyo",
}


def main():
    OUT.mkdir(exist_ok=True)
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME, headless=True,
            args=["--password-store=basic"],
        )
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        for name, url in PAGES.items():
            row = {"name": name, "url": url}
            t = time.time()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)  # let the JS settle
            except Exception as err:
                row["error"] = str(err)[:80]
                rows.append(row)
                continue
            row["goto_s"] = round(time.time() - t, 2)

            t = time.time()
            shot = page.screenshot()
            row["shot_s"] = round(time.time() - t, 2)
            row["shot_kb"] = len(shot) // 1024
            (OUT / f"{name}.png").write_bytes(shot)

            t = time.time()
            aria = page.locator("body").aria_snapshot()
            row["aria_s"] = round(time.time() - t, 2)
            row["aria_chars"] = len(aria)
            (OUT / f"{name}.aria.txt").write_text(aria)

            t = time.time()
            text = page.evaluate("document.body.innerText")
            row["text_s"] = round(time.time() - t, 2)
            row["text_chars"] = len(text)
            (OUT / f"{name}.text.txt").write_text(text)

            t = time.time()
            html = page.content()
            row["html_s"] = round(time.time() - t, 2)
            row["html_chars"] = len(html)

            rows.append(row)
            print(json.dumps(row), flush=True)
        browser.close()
    (OUT / "capture.json").write_text(json.dumps(rows, indent=2))


main()
