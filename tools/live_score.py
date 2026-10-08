"""Score every utterance the moment you finish saying it, on the Pi.

`score_wav.py` is the right tool once you have a take worth studying, but it
cannot tell you *why* an utterance missed, because by the time you read the
numbers you no longer remember which one was which. This closes that loop: talk,
watch the score, change how you say it, watch it move.

    uv run python tools/live_score.py ~/models/hey_gemini_fp05.onnx
    uv run python tools/live_score.py ~/models/hey_gemini_fp05.onnx hey_jarvis

Every utterance is also written to --save-dir with its score in the filename, so
a session doubles as data collection: the misses are exactly the clips
positive_train/ needs, and they arrive already sorted.

Model arguments are pretrained names or paths to a custom .onnx, same as
score_wav.py. Run tools/mic_setup.sh first.
"""

from __future__ import annotations

import argparse
import queue
import sys
import time
import wave
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd
from openwakeword.model import Model
from scipy.signal import resample_poly

# Imported from the listener rather than restated here. This script exists to
# predict what the listener will score, and a second copy of the frame size or
# the warmup could drift from it with nothing in the sweep able to notice.
from ai_terminal.wake import FRAME, SAMPLE_RATE, WARMUP_FRAMES

# openWakeWord scores the window that has just closed, so its peak lands well
# after you stop talking. Keep scoring this long past the end of an utterance
# before deciding it missed.
TAIL = 1.6
# Speech this far above the measured floor. 10 dB catches the room's own hum as
# a two-second "utterance"; Alex at 3 m lands 15-25 dB up.
GATE_MARGIN_DB = 15.0
# Bridge the stop between "hey" and "gemini" without splitting the utterance.
HANGOVER = 0.4
MIN_UTTERANCE = 0.25


def frame_level(frame: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
    return 20 * np.log10(rms / 32768) if rms > 0 else -99.0


def bar(score: float, width: int = 24) -> str:
    n = int(round(score * width))
    return "#" * n + "." * (width - n)


class Segmenter:
    """Splits the frame stream into utterances by energy, with a hangover.

    Emits (start_time, end_time) once an utterance has ended *and* TAIL seconds
    of further audio have been scored, so the caller always has the model's full
    response before it reports.
    """

    def __init__(self, gate: float) -> None:
        self.gate = gate
        self.start: float | None = None
        self.last_loud = 0.0
        self.pending: tuple[float, float] | None = None

    def push(self, t: float, level: float) -> tuple[float, float] | None:
        if level > self.gate:
            if self.start is None:
                self.start = t
            self.last_loud = t
        elif self.start is not None and t - self.last_loud > HANGOVER:
            if self.last_loud - self.start >= MIN_UTTERANCE:
                self.pending = (self.start, self.last_loud)
            self.start = None
        if self.pending and t - self.pending[1] > TAIL:
            done, self.pending = self.pending, None
            return done
        return None


def calibrate(stream_frames, seconds: float = 3.0) -> float:
    """Measure the room, so the gate does not have to be guessed per session.

    The 20th percentile, not the median: this room drifts about 10 dB minute to
    minute, and a 2 s median that lands on the loud end of that drift sets a gate
    high enough to swallow real speech.
    """
    levels = []
    print(f"measuring the room for {seconds:.0f}s — stay quiet...", flush=True)
    for t, level, _ in stream_frames:
        levels.append(level)
        if t >= seconds:
            break
    floor = float(np.percentile(levels, 20))
    print(f"floor {floor:.1f} dBFS  gate {floor + GATE_MARGIN_DB:.1f} dBFS\n", flush=True)
    return floor


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("models", nargs="+", help="pretrained names or paths to .onnx")
    p.add_argument("--device", default="seeed2micvoicec", help="input device name or index")
    p.add_argument("--rate", type=int, help="capture rate; default is the device's own")
    p.add_argument("--save-dir", default="~/utts", help="where to write per-utterance wavs")
    p.add_argument("--gate", type=float, help="fixed speech gate in dBFS; default is measured")
    args = p.parse_args(argv)

    device: str | int = int(args.device) if args.device.isdigit() else args.device
    info = sd.query_devices(device, "input")
    rate = int(args.rate or info["default_samplerate"])
    channels = min(int(info["max_input_channels"]), 2)

    save_dir = Path(args.save_dir).expanduser() / time.strftime("%Y%m%d-%H%M%S")
    save_dir.mkdir(parents=True, exist_ok=True)

    model = Model(wakeword_models=list(args.models), inference_framework="onnx")
    names = list(model.models)

    q: queue.Queue[np.ndarray] = queue.Queue()

    def cb(indata, _frames, _time, status):
        if status:
            print(f"audio status: {status}", file=sys.stderr)
        q.put(indata.copy())

    def frames():
        """Yield (elapsed_seconds, level_dBFS, frame) forever."""
        pending = np.empty(0, dtype=np.int16)
        n = 0
        warmup = WARMUP_FRAMES
        while True:
            block = q.get()
            mono = block.mean(axis=1) if block.ndim > 1 else block
            if rate != SAMPLE_RATE:
                mono = resample_poly(mono, SAMPLE_RATE, rate)
            pending = np.concatenate([pending, np.clip(mono, -32768, 32767).astype(np.int16)])
            while len(pending) >= FRAME:
                frame, pending = pending[:FRAME], pending[FRAME:]
                if warmup:
                    warmup -= 1
                    continue
                n += 1
                yield n * FRAME / SAMPLE_RATE, frame_level(frame), frame

    stream = sd.InputStream(
        device=device, samplerate=rate, channels=channels, dtype="int16",
        blocksize=int(rate * FRAME / SAMPLE_RATE), callback=cb,
    )
    print(f"{info['name']} @ {rate} Hz x{channels}ch -> {', '.join(names)}", flush=True)

    # Long enough to hold the loudest plausible utterance plus its whole tail.
    history: deque[tuple[float, np.ndarray, dict[str, float]]] = deque(maxlen=200)
    results: list[tuple[float, dict[str, float]]] = []
    # The per-utterance clips are convenient, but the segmenter's judgement is
    # baked into them. Keep the raw session too, so a run can be re-cut with a
    # different gate instead of re-recorded.
    session = wave.open(str(save_dir / "session.wav"), "wb")
    session.setnchannels(1)
    session.setsampwidth(2)
    session.setframerate(SAMPLE_RATE)

    with stream:
        f = frames()
        floor = args.gate - GATE_MARGIN_DB if args.gate else calibrate(f)
        seg = Segmenter(args.gate if args.gate else floor + GATE_MARGIN_DB)
        print("say the wake word. Ctrl-C when done.\n", flush=True)
        last_meter = -1
        try:
            for t, level, frame in f:
                scores = {k: float(v) for k, v in model.predict(frame).items()}
                session.writeframes(frame.tobytes())
                history.append((t, frame, scores))
                done = seg.push(t, level)
                if not done:
                    # An idle meter, so silence reads as "listening" rather than
                    # "wedged". Overwritten in place; results scroll past it.
                    if int(t / 0.24) != last_meter:
                        last_meter = int(t / 0.24)
                        live = "*" if seg.start else " "
                        print(f"\r   {live} {level:6.1f} dBFS  [{bar(max(scores.values()))}]",
                              end="", flush=True)
                    continue
                print("\r" + " " * 60 + "\r", end="")
                start, end = done
                # The peak can only be attributed to this utterance if it falls
                # between its onset and the end of the tail.
                window = [h for h in history if start <= h[0] <= end + TAIL]
                peak = {n: max(h[2][n] for h in window) for n in names}
                # Out to the end of the tail, not the end of the speech. The
                # model's peak lands inside that tail, so a clip trimmed at
                # end + 0.5 re-scores as a miss even when it fired live — the
                # window that produced the peak is no longer in the file.
                audio = np.concatenate(
                    [h[1] for h in history if start - 0.5 <= h[0] <= end + TAIL + 0.3]
                )
                loud = max(frame_level(h[1]) for h in window)
                best = max(peak.values())
                tag = f"{best:.3f}".replace(".", "")
                path = save_dir / f"{len(results):03d}_{tag}.wav"
                with wave.open(str(path), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(SAMPLE_RATE)
                    w.writeframes(audio.tobytes())
                results.append((loud, peak))
                cols = "  ".join(f"{n} {peak[n]:.3f}" for n in names)
                print(
                    f"{len(results):3d}  {end - start:4.2f}s  {loud:6.1f} dBFS  "
                    f"[{bar(best)}]  {cols}",
                    flush=True,
                )
        except KeyboardInterrupt:
            pass
        finally:
            session.close()

    if not results:
        print("\nno utterances — was the gate too high? try --gate")
        return 0
    print(f"\n{len(results)} utterances, saved to {save_dir}")
    for n in names:
        peaks = np.array([r[1][n] for r in results])
        hits = "  ".join(
            f"thr {t:.2f} {int((peaks >= t).sum())}/{len(peaks)}" for t in (0.5, 0.3, 0.2, 0.1, 0.05)
        )
        print(f"  {n:24s} median {np.median(peaks):.3f}  {hits}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
