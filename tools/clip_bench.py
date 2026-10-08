"""Rank wake-word models on saved real utterances.

tools/rir_bench.py convolves dry clips through impulse responses, which is the
only way to iterate without a human in the kitchen — but it is still a
simulation, and every model here was trained on impulse responses too. This
scores the recordings themselves: whatever tools/live_score.py saved while
somebody stood in the actual spot and said the actual phrase.

    uv run python tools/clip_bench.py ~/utts/hat_panel/20260908-050728 \
        ~/models/hey_gemini_ls128.onnx ~/models/hey_gemini_kitchen.onnx

Those takes are the held-out set in the strictest sense available: no real
recording has ever entered training. Nothing else here is — the MIT rooms are
what the models were augmented with, and the validation set is Piper scored
against Piper. When this table and any other disagree, this one is right.

Clips are counted as hits at 0.2, the threshold the Pi runs, and the median is
reported alongside because a model can move every clip from 0.001 to 0.1 —
real progress — without the count changing at all.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

from rir_bench import USABLE, Scorer, read_clip


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("clips", help="directory of saved utterances")
    p.add_argument("models", nargs="+", help="paths to .onnx, or pretrained names")
    p.add_argument("--per-clip", action="store_true", help="print every clip, not just the summary")
    args = p.parse_args(argv)

    paths = sorted(Path(args.clips).expanduser().glob("*.wav"))
    if not paths:
        raise SystemExit(f"no wavs in {args.clips}")
    clips = [read_clip(q) for q in paths]
    print(f"{len(clips)} clips from {args.clips}\n")

    for m in args.models:
        s = Scorer(m)
        v = np.array([s.peak(c) for c in clips])
        if args.per_clip:
            for q, x in zip(paths, v):
                print(f"   {x:.3f}  {q.name}")
        print(
            f"{Path(m).stem:28s} {int((v >= USABLE).sum()):2d}/{len(v)} at {USABLE}  "
            f"median {np.median(v):.3f}  max {v.max():.3f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
