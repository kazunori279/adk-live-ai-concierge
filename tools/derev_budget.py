"""How many dB of dereverberation would the wake word actually need?

"Would a beamformer help?" is not answerable by arguing about beamformers. It is
answerable by asking the model. Take the measured impulse response, suppress its
reverberant part by N dB, convolve the dry clips through the result, and score
them: that is a perfect dereverberator with a knob on it, and it puts an upper
bound on every real front end. If the score is still dead at 12 dB, no two-mic
array is going to save it, because two capsules do not buy 12 dB.

    uv run python tools/derev_budget.py ~/dry50 ~/models/hey_gemini_ls128.onnx \
        --rir training/rirs/kitchen_panel.wav

Two columns, because they are two different machines:

  all     the whole reverberant part attenuated, early reflections included.
          This is what a beamformer does — it rejects by direction, and an early
          reflection arrives from a wall, not from the talker.
  late    only what arrives more than 50 ms after the direct sound. This is what
          WPE-style dereverberation does; it estimates and subtracts the tail
          and leaves the early reflections, which carry useful speech energy.

The gap between the two columns is worth reading. If `late` alone recovers the
score, the tail is the problem and single-channel dereverberation is on the
table. If only `all` recovers it, the damage is in the early reflections, where
a 5.6 cm two-mic array has almost no directivity to work with.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import fftconvolve

from rir_bench import SAMPLE_RATE, Scorer, read_clip, read_rir, rt60

# The conventional early/late boundary for speech (the C50 one). Reflections
# inside it fuse with the direct sound and help intelligibility; past it they
# smear the next phoneme over the last.
LATE_MS = 50.0
DIRECT_MS = 2.5


def parts(h: np.ndarray) -> tuple[int, np.ndarray, np.ndarray, np.ndarray]:
    """Direct, early and late, as three full-length arrays that sum back to h."""
    p = int(np.argmax(np.abs(h)))
    d = np.zeros_like(h)
    w = int(SAMPLE_RATE * DIRECT_MS / 1000)
    d[max(0, p - w) : p + w] = h[max(0, p - w) : p + w]
    late = np.zeros_like(h)
    q = p + int(SAMPLE_RATE * LATE_MS / 1000)
    late[q:] = h[q:]
    return p, d, h - d - late, late


def drr(h: np.ndarray) -> float:
    _, d, e, late = parts(h)
    rev = float(np.sum(e**2) + np.sum(late**2))
    return 10 * np.log10(float(np.sum(d**2)) / rev) if rev > 0 else 99.0


def suppress(h: np.ndarray, db: float, mode: str) -> np.ndarray:
    g = 10 ** (-db / 20)
    _, d, e, late = parts(h)
    return d + (e + late) * g if mode == "all" else d + e + late * g


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("clips", help="directory of dry close-mic wavs")
    p.add_argument("models", nargs="+", help="paths to .onnx, or pretrained names")
    p.add_argument("--rir", required=True, help="the measured impulse response")
    p.add_argument("--db", type=float, nargs="+", default=[0, 3, 6, 9, 12, 15, 20, 30])
    args = p.parse_args(argv)

    h = read_rir(Path(args.rir).expanduser())
    print(f"{Path(args.rir).name}: RT60 {rt60(h):.2f}s  DRR {drr(h):+.1f} dB\n")
    clips = [read_clip(q) for q in sorted(Path(args.clips).expanduser().glob("*.wav"))]

    for m in args.models:
        s = Scorer(m)
        print(f"== {Path(m).stem}  dry {np.median([s.peak(c) for c in clips]):.3f}")
        print(f"   {'suppress':>9}  {'all':>17}  {'late only':>17}")
        for db in args.db:
            row = []
            for mode in ("all", "late"):
                g = suppress(h, db, mode)
                v = np.array([s.peak(fftconvolve(c, g)) for c in clips])
                row.append(f"{np.median(v):.3f} ({int((v >= 0.2).sum())}/{len(v)}) {drr(g):+5.1f}")
            print(f"   {db:6.0f} dB  {row[0]:>17}  {row[1]:>17}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
