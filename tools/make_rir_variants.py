"""Turn one measured impulse response into a training-sized set of neighbours.

tools/measure_ir.py produces a single file for a single spot on the kitchen
floor, and openWakeWord's augmenter picks RIRs with `random.choice` over a flat
list — so dropping that file into mit_rirs/ gives it a 1-in-270 chance of ever
being used, and pointing the augmenter at it alone teaches the model one exact
position and one exact head angle. Neither is what we want. This writes N
perturbed copies, so "the room the terminal lives in" becomes a region rather
than a point.

    uv run python tools/make_rir_variants.py training/rirs/kitchen_panel.wav \
        ~/kitchen_rirs --n 270

The perturbations are the three things that actually move between one utterance
and the next, and nothing else:

  DRR     is the axis the wake word dies on. The measurement says -3.6 dB at
          2.5 m; scaling the reverberant part covers standing nearer or further,
          which is the difference between a model that works at one tile of the
          floor and one that works in the kitchen.
  RT60    varies with how much soft furniture and how many people are in the
          room. An exponential on the tail stretches or shrinks it.
  tilt    covers the panel's colouration changing with head angle, and stops the
          model from memorising one comb pattern as if it were the wake word.

Nothing here invents early reflections that were not measured: a small resample
moves every arrival together, the way a slightly different room would, rather
than sprinkling in reflections that no wall could have produced.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly

RATE = 16_000
DIRECT_MS = 2.5  # the conventional direct-sound window, same as measure_ir.py


def split(h: np.ndarray, peak: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Direct sound and everything after it, as two full-length arrays.

    `peak` has to be passed once the reverberant part has been scaled up: at the
    measured DRR of -3.6 dB the tail already carries more energy than the direct
    sound, so a few dB more and argmax lands on a reflection. Every subsequent
    measurement then reads from the wrong onset, which is what made set_drr(-12)
    quietly return -8.7 dB.
    """
    p = int(np.argmax(np.abs(h))) if peak is None else peak
    w = int(RATE * DIRECT_MS / 1000)
    mask = np.zeros_like(h)
    mask[max(0, p - w) : p + w] = 1.0
    return h * mask, h * (1 - mask)


def drr(h: np.ndarray, peak: int | None = None) -> float:
    d, r = split(h, peak)
    late = float(np.sum(r**2))
    return 10 * np.log10(float(np.sum(d**2)) / late) if late > 0 else 99.0


def rt60(h: np.ndarray) -> float:
    e = np.cumsum(h[::-1] ** 2)[::-1]
    db = 10 * np.log10(e / e.max() + 1e-20)
    a, b = int(np.argmax(db < -5)), int(np.argmax(db < -25))
    return (b - a) / RATE * 3 if b > a else 0.0


def set_drr(h: np.ndarray, target: float, peak: int) -> np.ndarray:
    d, r = split(h, peak)
    return d + r * 10 ** ((drr(h, peak) - target) / 20)


def set_rt60(h: np.ndarray, factor: float, peak: int) -> np.ndarray:
    """Stretch or shrink the decay by `factor`, leaving the direct sound alone.

    RT60 is 60 dB of *energy*, which is 30 dB of amplitude, so the envelope is
    exp(-t * ln(1e3) / T) and moving T to factor*T means multiplying by
    exp(-t * ln(1e3) / T * (1/factor - 1)). Using ln(1e6) here — the energy
    figure, against an amplitude envelope — makes every adjustment twice as
    strong as asked, which reads as the knob simply not working.

    Only shortening is offered, and that is a property of the measurement, not
    a preference. A factor above 1 is gain on the last samples, and the take
    only reaches -48 dB at 0.7 s: asking for 1.1x lifted the recorder's hiss far
    enough that RT60 read 1.46 s against the 0.85 s requested. A room more
    reverberant than the one measured is not in this file to be recovered.

    Shortening undershoots a little — 0.75x gives 0.49 s where the arithmetic
    says 0.58 s — because the decay is not one clean exponential. That is fine
    for augmentation, where the point is spread rather than an exact target.
    """
    t0 = rt60(h)
    if t0 <= 0 or abs(factor - 1) < 1e-3:
        return h
    d, r = split(h, peak)
    t = np.arange(len(h)) / RATE
    return d + r * np.exp(-t * np.log(1e3) / t0 * (1 / factor - 1))


def tilt(h: np.ndarray, db_per_octave: float) -> np.ndarray:
    f = np.fft.rfftfreq(len(h), 1 / RATE)
    octaves = np.log2(np.maximum(f, 20.0) / 1000.0)
    return np.fft.irfft(np.fft.rfft(h) * 10 ** (db_per_octave * octaves / 20), len(h))


def stretch(h: np.ndarray, factor: float) -> np.ndarray:
    """Resample, i.e. move every arrival time together, as a bigger room would."""
    num = int(round(1000 * factor))
    out = resample_poly(h, num, 1000)
    return out[: len(h)] if len(out) >= len(h) else np.pad(out, (0, len(h) - len(out)))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("source", help="measured impulse response from measure_ir.py")
    p.add_argument("out_dir")
    p.add_argument("--n", type=int, default=270, help="match the MIT set for a 50/50 mix")
    # Centred on the measurement, wide enough to cover the whole kitchen: +4 dB
    # is roughly arm's length, -12 dB is the far corner.
    p.add_argument("--drr", type=float, nargs=2, default=(-12.0, 4.0))
    p.add_argument("--rt60-factor", type=float, nargs=2, default=(0.5, 1.0))
    p.add_argument("--tilt", type=float, nargs=2, default=(-3.0, 3.0))
    p.add_argument("--stretch", type=float, nargs=2, default=(0.97, 1.03))
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)

    sr, h = wavfile.read(args.source)
    if sr != RATE:
        raise SystemExit(f"{args.source}: expected {RATE} Hz, got {sr}")
    h = h.astype(np.float64)
    h = h.mean(1) if h.ndim > 1 else h
    print(f"source: RT60 {rt60(h):.2f}s  DRR {drr(h):+.1f} dB")

    out = Path(args.out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    made = []
    for i in range(args.n):
        v = stretch(h, rng.uniform(*args.stretch))
        v = tilt(v, rng.uniform(*args.tilt))
        # Pin the onset here, before the tail is scaled, and carry it through.
        peak = int(np.argmax(np.abs(v)))
        v = set_rt60(v, rng.uniform(*args.rt60_factor), peak)
        v = set_drr(v, rng.uniform(*args.drr), peak)
        v = (v / np.abs(v).max() * 0.9).astype(np.float32)
        wavfile.write(out / f"{Path(args.source).stem}_{i:03d}.wav", RATE, v)
        made.append((v, peak))

    d = np.array([drr(v, p) for v, p in made])
    # Of the reverberant part alone. Whole-IR RT60 moves when set_drr changes
    # how far the Schroeder curve has to fall before it reaches the tail, so it
    # reports the DRR knob a second time instead of reporting the decay.
    t = np.array([rt60(split(v, p)[1][p:]) for v, p in made])
    print(f"{args.n} variants in {out}")
    print(f"  DRR       {d.min():+.1f} .. {d.max():+.1f} dB, median {np.median(d):+.1f}")
    print(f"  tail RT60 {t.min():.2f} .. {t.max():.2f} s, median {np.median(t):.2f}  "
          f"(source {rt60(split(h)[1][int(np.argmax(np.abs(h))):]):.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
