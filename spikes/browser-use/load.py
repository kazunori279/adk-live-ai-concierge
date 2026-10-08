"""What a second, agent-driven Chromium costs the Pi.

Kiosk Chromium is already on the display. This launches a headless one, loops
screenshots at a step-like cadence, and samples memory, CPU, and SoC temperature
while an ALSA capture runs in parallel so overruns show up if the audio pipeline
starts losing its slot.
"""
import json
import subprocess
import time

from playwright.sync_api import sync_playwright

CHROME = "/usr/bin/chromium"
URL = "https://news.ycombinator.com"
STEPS = 30
PERIOD = 2.0


def sample():
    mem = subprocess.run(["free", "-m"], capture_output=True, text=True).stdout
    used = int(mem.splitlines()[1].split()[2])
    temp = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True,
                          text=True).stdout.strip().split("=")[1]
    load = open("/proc/loadavg").read().split()[0]
    return {"used_mb": used, "temp": temp, "load1": float(load)}


def main():
    print(json.dumps({"baseline": sample()}), flush=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME, headless=True,
            args=["--password-store=basic"],
        )
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        print(json.dumps({"loaded": sample()}), flush=True)

        shots = []
        start = time.time()
        for i in range(STEPS):
            t = time.time()
            page.screenshot()
            shots.append(time.time() - t)
            if i in (9, 19, 29):
                print(json.dumps({"step": i + 1, **sample()}), flush=True)
            time.sleep(max(0, PERIOD - (time.time() - t)))
        wall = time.time() - start

        print(json.dumps({
            "shots": STEPS,
            "shot_avg_s": round(sum(shots) / len(shots), 3),
            "shot_max_s": round(max(shots), 3),
            "wall_s": round(wall, 1),
            **sample(),
        }), flush=True)
        browser.close()
    time.sleep(2)
    print(json.dumps({"after": sample()}), flush=True)


main()
