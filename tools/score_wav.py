"""Score a recording with openWakeWord and report peaks, SNR and activations.

The bring-up loop for a wake word is "record once, then tune offline". Asking a
human to speak into a fixed window wastes their time and produces one data
point; a saved take can be re-scored against every model and threshold for
free. Run this on the Pi, where openWakeWord is installed.

    uv run python tools/score_wav.py far3m.wav hey_jarvis
    uv run python tools/score_wav.py far3m.wav ~/models/hey_gemini.onnx

Model arguments are either pretrained names or paths to a custom .onnx.
"""

from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np
from openwakeword.model import Model

SAMPLE_RATE = 16_000
FRAME = 1280
# The band that carries the wake word. Below it sits room rumble, above it the
# codec's own hiss; measuring the floor over the full band flatters both.
BAND = (300, 4000)


def read_mono(path: str) -> np.ndarray:
    w = wave.open(path)
    if w.getframerate() != SAMPLE_RATE:
        raise SystemExit(f"{path}: expected {SAMPLE_RATE} Hz, got {w.getframerate()}")
    d = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64)
    if w.getnchannels() > 1:
        d = d.reshape(-1, w.getnchannels()).mean(1)
    return d - d.mean()


def snr(x: np.ndarray) -> tuple[float, float]:
    """Speech-band floor and peak in dBFS, as a stand-in for talker distance."""
    n = 1600
    freqs = np.fft.rfftfreq(n, 1 / SAMPLE_RATE)
    band = (freqs > BAND[0]) & (freqs < BAND[1])
    win = np.hanning(n)
    energy = [
        10 * np.log10(np.abs(np.fft.rfft(x[i : i + n] * win))[band].__pow__(2).sum() / 32768**2 + 1e-30)
        for i in range(0, len(x) - n, n)
    ]
    e = np.array(energy)
    return float(np.percentile(e, 20)), float(np.percentile(e, 98))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("wav")
    ap.add_argument("models", nargs="+", help="pretrained names or paths to .onnx")
    ap.add_argument("--min-peak", type=float, default=0.05, help="ignore peaks below this")
    args = ap.parse_args()

    x = read_mono(args.wav)
    floor, peak = snr(x)
    print(
        f"{Path(args.wav).name}: {len(x)/SAMPLE_RATE:.0f}s  "
        f"floor {floor:.1f} dB  peak {peak:.1f} dB  SNR {peak-floor:+.1f} dB  "
        f"max |x| {np.abs(x).max()/32768:.2f} FS"
    )

    y = np.clip(x, -32768, 32767).astype(np.int16)
    mdl = Model(wakeword_models=list(args.models), inference_framework="onnx")
    times: list[float] = []
    scores: dict[str, list[float]] = {m: [] for m in mdl.models}
    for i in range(0, len(y) - FRAME, FRAME):
        p = mdl.predict(y[i : i + FRAME])
        times.append((i + FRAME) / SAMPLE_RATE)
        for m in mdl.models:
            scores[m].append(p[m])

    for m in mdl.models:
        s = np.array(scores[m])
        print(f"== {m}   max {s.max():.3f}")
        taken: list[float] = []
        for i in np.argsort(-s):
            if s[i] < args.min_peak or len(taken) >= 25:
                break
            if all(abs(times[i] - t) > 1.5 for t in taken):
                taken.append(times[i])
                print(f"   {times[i]:6.1f}s  {s[i]:.3f}")
        for th in (0.5, 0.3, 0.2, 0.1, 0.05):
            n, last = 0, -9.0
            for i, v in enumerate(s):
                if v >= th and times[i] - last > 2:
                    n, last = n + 1, times[i]
            print(f"   thr {th:.2f} -> {n} activations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
