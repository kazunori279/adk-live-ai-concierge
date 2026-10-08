"""How many dB of noise suppression would the wake word actually need?

Mix dry utterances with interferers (music, TV speech, background noise) across
a sweep of SNRs, suppress the interferer by N dB, re-score with openWakeWord,
and measure how much suppression is required to recover recall.

    uv run python tools/noise_budget.py ~/dry50 ~/models/hey_gemini_ls128.onnx \\
        --noise ~/noise/kitchen_noise.wav
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

from rir_bench import SAMPLE_RATE, USABLE, Scorer, read_clip


def mix_at_snr(speech: np.ndarray, noise: np.ndarray, snr_db: float) -> tuple[np.ndarray, np.ndarray]:
    """Given 1D float32 speech and noise audio arrays, repeat/trim noise to match
    len(speech), calculate power of each, scale noise so 10*log10(P_speech/P_noise_scaled) = snr_db,
    return (speech, scaled_noise).
    """
    speech = np.asarray(speech, dtype=np.float32).reshape(-1)
    noise = np.asarray(noise, dtype=np.float32).reshape(-1)
    n = len(speech)
    if len(noise) < n:
        reps = int(np.ceil(n / len(noise))) if len(noise) > 0 else 1
        noise = np.tile(noise, reps)[:n] if len(noise) > 0 else np.zeros(n, dtype=np.float32)
    elif len(noise) > n:
        noise = noise[:n]

    p_speech = float(np.mean(speech ** 2))
    p_noise = float(np.mean(noise ** 2))
    if p_speech <= 0.0 or p_noise <= 0.0:
        return speech, np.zeros_like(speech)

    scale = np.sqrt(p_speech / (p_noise * (10.0 ** (snr_db / 10.0))))
    return speech, (noise * scale).astype(np.float32)


def suppress_noise(noise: np.ndarray, db: float) -> np.ndarray:
    """Attenuate noise by 10^(-db / 20)."""
    noise = np.asarray(noise, dtype=np.float32)
    g = 10.0 ** (-db / 20.0)
    return (noise * g).astype(np.float32)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("clips", help="directory of dry close-mic wav files")
    p.add_argument("models", nargs="+", help="paths to .onnx models or model names")
    p.add_argument("--noise", default=None, help="path to noise wav file or directory of noise wavs")
    p.add_argument("--snr", type=float, nargs="+", default=[-5.0, 0.0, 5.0, 10.0, 15.0],
                   help="list of SNR dB values")
    p.add_argument("--db", type=float, nargs="+", default=[0.0, 3.0, 6.0, 9.0, 12.0, 15.0, 20.0],
                   help="list of suppression dB values")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if not args.noise:
        raise SystemExit("--noise is required")

    noise_p = Path(args.noise).expanduser()
    if noise_p.is_dir():
        noise_files = sorted(noise_p.glob("*.wav"))
        if not noise_files:
            raise SystemExit(f"no wavs in {args.noise}")
        noise_data = np.concatenate([read_clip(f) for f in noise_files])
    else:
        if not noise_p.exists():
            raise SystemExit(f"noise file not found: {args.noise}")
        noise_data = read_clip(noise_p)

    clip_paths = sorted(Path(args.clips).expanduser().glob("*.wav"))
    if not clip_paths:
        raise SystemExit(f"no wavs in {args.clips}")
    clips = [read_clip(q) for q in clip_paths]

    noise_data = noise_data.astype(np.float32)

    for m in args.models:
        s = Scorer(m)
        dry_peaks = [s.peak(c) for c in clips]
        print(f"\n== {Path(m).stem}  dry median {np.median(dry_peaks):.3f} ({int((np.array(dry_peaks) >= USABLE).sum())}/{len(clips)})")

        headers = [f"SNR {snr:+.0f} dB" for snr in args.snr]
        header_line = f"   {'suppress':>9}  " + "  ".join(f"{h:>17}" for h in headers)
        print(header_line)

        for db in args.db:
            row = []
            for snr in args.snr:
                peaks = []
                offset = 0
                for c in clips:
                    n_len = len(c)
                    if len(noise_data) <= n_len:
                        n_chunk = noise_data
                    else:
                        if offset + n_len > len(noise_data):
                            offset = 0
                        n_chunk = noise_data[offset : offset + n_len]
                        offset += n_len
                    sp, sc_n = mix_at_snr(c, n_chunk, snr)
                    mixed = sp + suppress_noise(sc_n, db)
                    peaks.append(s.peak(mixed))
                v = np.array(peaks)
                usable_count = int((v >= USABLE).sum())
                row.append(f"{np.median(v):.3f} ({usable_count}/{len(v)})")
            print(f"   {db:6.0f} dB  " + "  ".join(f"{col:>17}" for col in row))

    return 0


if __name__ == "__main__":
    sys.exit(main())
