"""Rank wake-word models by how many real rooms they survive.

openWakeWord's own validation set is synthetic on both sides — Piper speech
scored against Piper speech — and it picked the wrong model here: it ranked
`fp20` over `fp05`, while on Alex's actual voice `fp05` won 4 hits to 2. This is
the replacement metric. Take clips of the real speaker recorded close and dry,
convolve them through the MIT impulse responses the model was trained against,
and count the rooms that still work.

    uv run python tools/rir_bench.py ~/utts/20260907-141303 ~/models/hey_gemini_fp05.onnx

Dry clips come from tools/live_score.py at ~50 cm, where scores sit at 0.95 and
the recording is all direct sound. Pass --min-dry to drop anything that was not
a clean utterance to begin with; a clip the model never heard dry says nothing
about rooms.

The impulse responses download on first use into --rir-dir.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import wave
from pathlib import Path

import numpy as np
from openwakeword.model import Model
from scipy.io import wavfile
from scipy.signal import fftconvolve

REPO = "davidscripka/MIT_environmental_impulse_responses"
TREE = f"https://huggingface.co/api/datasets/{REPO}/tree/main/16khz"
RAW = f"https://huggingface.co/datasets/{REPO}/resolve/main/16khz"
SAMPLE_RATE = 16_000
FRAME = 1280
# A room counts as usable at the threshold the Pi would actually run, not at
# openWakeWord's nominal 0.5: false positives sit at 0.001 over five minutes of
# this room, so there is no reason to ask for more.
USABLE = 0.2


def fetch_rirs(dest: Path, limit: int) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    have = sorted(dest.glob("*.wav"))
    if len(have) >= limit:
        return have[:limit]
    with urllib.request.urlopen(TREE) as r:
        names = [f["path"].split("/")[-1] for f in json.load(r)][:limit]
    for n in names:
        p = dest / n
        if not p.exists():
            print(f"  fetching {n}", file=sys.stderr)
            urllib.request.urlretrieve(f"{RAW}/{n}", p)
    return sorted(dest.glob("*.wav"))[:limit]


def read_clip(p: Path) -> np.ndarray:
    w = wave.open(str(p))
    if w.getframerate() != SAMPLE_RATE:
        raise SystemExit(f"{p}: expected {SAMPLE_RATE} Hz, got {w.getframerate()}")
    d = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64)
    return d.reshape(-1, w.getnchannels()).mean(1) if w.getnchannels() > 1 else d


def read_rir(p: Path) -> np.ndarray:
    # The MIT files are 32-bit float, which the wave module cannot unpack.
    _, h = wavfile.read(str(p))
    h = h.astype(np.float64)
    return h.mean(1) if h.ndim > 1 else h


def rt60(h: np.ndarray) -> float:
    """Schroeder backward integration, -5 to -25 dB extrapolated to -60."""
    e = np.cumsum(h[::-1] ** 2)[::-1]
    db = 10 * np.log10(e / e.max() + 1e-20)
    a, b = int(np.argmax(db < -5)), int(np.argmax(db < -25))
    return (b - a) / SAMPLE_RATE * 3 if b > a else 0.0


class Scorer:
    def __init__(self, model_path: str) -> None:
        self.model = Model(wakeword_models=[model_path], inference_framework="onnx")
        self.name = list(self.model.models)[0]
        # openWakeWord scores the window that has just closed, so a clip fed
        # cold reports whatever its buffer held. A second of silence either side
        # gives every clip the same start and lets the peak land.
        self.pad = np.zeros(SAMPLE_RATE, dtype=np.int16)

    def peak(self, x: np.ndarray) -> float:
        y = (x / max(np.abs(x).max(), 1e-9) * 8000).astype(np.int16)
        y = np.concatenate([self.pad, y, self.pad])
        self.model.reset()
        return max(
            float(self.model.predict(y[j : j + FRAME])[self.name])
            for j in range(0, len(y) - FRAME, FRAME)
        )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("clips", help="directory of dry close-mic wavs")
    p.add_argument("models", nargs="+", help="paths to .onnx, or pretrained names")
    p.add_argument("--rir-dir", default="~/mit_rirs", help="impulse response cache")
    p.add_argument("--n-rirs", type=int, default=40, help="how many rooms to test")
    p.add_argument("--min-dry", type=float, default=0.5, help="drop clips weaker than this dry")
    p.add_argument("--per-room", action="store_true", help="print every room, not just the summary")
    args = p.parse_args(argv)

    clip_paths = sorted(Path(args.clips).expanduser().glob("*.wav"))
    if not clip_paths:
        raise SystemExit(f"no wavs in {args.clips}")
    rirs = [(read_rir(r), r.name) for r in fetch_rirs(Path(args.rir_dir).expanduser(), args.n_rirs)]

    for model_path in args.models:
        s = Scorer(model_path)
        clips = [read_clip(c) for c in clip_paths]
        dry = [s.peak(c) for c in clips]
        keep = [c for c, d in zip(clips, dry) if d >= args.min_dry]
        print(f"\n== {Path(model_path).stem}")
        print(f"   dry: median {np.median(dry):.3f} over {len(clips)} clips, "
              f"{len(keep)} above {args.min_dry}")
        if not keep:
            print("   no clip survives dry — wrong model, or the clips are not the phrase")
            continue
        rows = []
        for h, name in rirs:
            peaks = [s.peak(fftconvolve(c, h)) for c in keep]
            rows.append((rt60(h), float(np.median(peaks)), name))
        if args.per_room:
            for t, med, name in sorted(rows):
                mark = "ok  " if med >= USABLE else "dead"
                print(f"   RT60 {t:4.2f}s  {med:5.3f}  {mark}  {name[:44]}")
        meds = np.array([r[1] for r in rows])
        print(f"   rooms: median {np.median(meds):.3f}, "
              f"{int((meds >= USABLE).sum())}/{len(rows)} usable at {USABLE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
