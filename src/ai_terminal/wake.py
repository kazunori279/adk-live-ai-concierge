"""Wake-word listener built on openWakeWord.

Runs openWakeWord's ONNX path. The tflite path would be a little faster, but
tflite-runtime has no wheel for the Python 3.13 that Raspberry Pi OS Trixie
ships; see the `override-dependencies` note in pyproject.toml.

The microphone is a ReSpeaker 2-Mics Pi HAT V2.0, which captures stereo at
whatever rate its TLV320AIC3104 codec is configured for. openWakeWord wants
16 kHz mono, so the capture stream is downmixed and resampled here rather than
relying on ALSA plug conversion, which adds latency and hides errors.

    python -m ai_terminal.wake --list-devices
    python -m ai_terminal.wake --model hey_jarvis --device seeed2micvoicec
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import queue
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

# Above the openWakeWord import, and that is the whole of the point.
# `openwakeword/__init__.py` pulls in `openwakeword/vad.py:48`, which is
# `import onnxruntime`, and the official onnxruntime wheels for macOS and Linux
# link Microsoft's 1DS client (cpp_client_telemetry, as `libmat.a`) and start it
# as the extension loads: two worker threads that upload AppInfo, DeviceInfo,
# UserInfo.Id and Session.FirstLaunchTime from this household's Pi on every
# restart of the monitor. See `Privacy.md` in the installed wheel.
#
# It also aborts the process. Around ten seconds after the import the uploader
# sends its first batch, and if the process exits while that is in flight the
# static destructor tearing the client down at exit() and the worker decoding
# the response race: the worker locks a recursive_mutex that has already gone,
# libc++ throws out of a noexcept path, and the run ends on SIGABRT — exit 134,
# after every line of output it was going to print. `tools/deploy_check.py:105`
# and `tools/wake_check.py:63` both import this module, so that abort lands on
# a check script, and `tools/check_all.py:106` scores a script that printed its
# count and then returned 134 as a crash rather than as the pass it was.
#
# `setdefault` and not an assignment: onnxruntime parses the value rather than
# looking for the name, so `0`, `2` and the empty string all leave telemetry
# running, and a developer who set one of those meant it. It has to be in the
# environment *before* the import — the variable is read once as the extension
# loads, and `onnxruntime.disable_telemetry_events()` afterwards leaves the
# client and its threads exactly where they are.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

# pylint: disable=wrong-import-position
import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402
import openwakeword.utils  # noqa: E402
from openwakeword.model import Model  # noqa: E402
from scipy.signal import resample_poly  # noqa: E402

SAMPLE_RATE = 16_000
# openWakeWord's melspectrogram frontend expects 80 ms frames at 16 kHz.
FRAME = 1280
# The TLV320AIC3104 emits a full-scale pop for the first moment of a stream.
# Scoring it just primes the model with a transient, so drop it.
WARMUP_FRAMES = 13
# Never longer than the default refractory, so a hanging agent cannot stall
# scoring. Equal to it today, which is what "inside" means here.
WEBHOOK_TIMEOUT = 2.0

WakeCallback = Callable[[str, float], None]


def list_devices() -> str:
    return str(sd.query_devices())


def ensure_models() -> None:
    """Fetch the pretrained models, which the wheel does not ship.

    openWakeWord downloads them on first use into its own package directory, so
    they vanish whenever the venv is rebuilt and `Model()` then dies with a bare
    NO_SUCHFILE. Checking here turns that into a one-off download.
    """
    models = Path(openwakeword.utils.__file__).parent / "resources" / "models"
    if not any(models.glob("*.onnx")):
        print("downloading openWakeWord models (one-off)...", flush=True)
        openwakeword.utils.download_models()


def _downmix_resample(block: np.ndarray, rate: int) -> np.ndarray:
    """Turn a capture block into 16 kHz mono int16."""
    mono = block.mean(axis=1) if block.ndim > 1 else block
    if rate != SAMPLE_RATE:
        mono = resample_poly(mono, SAMPLE_RATE, rate)
    return np.clip(mono, -32768, 32767).astype(np.int16)


class WakeListener:
    """Streams the microphone through openWakeWord and fires on detections.

    `refractory` suppresses repeat hits from a single utterance: after a
    detection the model's audio buffers are cleared and scoring pauses, so one
    "hey jarvis" produces one callback instead of a burst.
    """

    def __init__(
        self,
        on_wake: WakeCallback,
        models: Sequence[str] = ("hey_jarvis",),
        threshold: float = 0.5,
        vad_threshold: float = 0.0,
        refractory: float = 2.0,
        device: str | int | None = None,
        capture_rate: int | None = None,
        debug: bool = False,
    ) -> None:
        self.on_wake = on_wake
        self.threshold = threshold
        self.refractory = refractory
        self.device = device
        self.debug = debug

        info = sd.query_devices(device, "input")
        self.capture_rate = int(capture_rate or info["default_samplerate"])
        self.channels = min(int(info["max_input_channels"]), 2)
        if self.channels < 1:
            raise RuntimeError(f"input device has no capture channels: {info['name']}")

        ensure_models()
        self.model = Model(
            wakeword_models=list(models),
            inference_framework="onnx",
            vad_threshold=vad_threshold,
        )
        self._queue: queue.Queue[np.ndarray] = queue.Queue()
        self._pending = np.empty(0, dtype=np.int16)
        self._muted_until = 0.0
        self._warmup = WARMUP_FRAMES
        self._debug_best = ("", 0.0, 0.0)  # name, score, input level
        self._debug_n = 0

    def _callback(self, indata, _frames, _time, status) -> None:
        if status:
            print(f"audio status: {status}", file=sys.stderr)
        self._queue.put(indata.copy())

    def run(self) -> None:
        # 80 ms of capture-rate audio per block keeps the resampler cheap and
        # lines up with the frame size once converted.
        blocksize = int(self.capture_rate * FRAME / SAMPLE_RATE)
        stream = sd.InputStream(
            device=self.device,
            samplerate=self.capture_rate,
            channels=self.channels,
            dtype="int16",
            blocksize=blocksize,
            callback=self._callback,
        )
        # The stream the constructor warmed up for is not the only one this
        # listener ever opens: `run` is re-entered whenever the graph beneath
        # it goes away and comes back, and every new stream opens with the
        # codec's step again.
        self._queue = queue.Queue()
        self._pending = np.empty(0, dtype=np.int16)
        self._warmup = WARMUP_FRAMES
        with stream:
            while True:
                block = self._queue.get()
                self._pending = np.concatenate(
                    [self._pending, _downmix_resample(block, self.capture_rate)]
                )
                while len(self._pending) >= FRAME:
                    frame, self._pending = self._pending[:FRAME], self._pending[FRAME:]
                    self._score(frame)

    def _score(self, frame: np.ndarray) -> None:
        if self._warmup:
            self._warmup -= 1
            return
        scores = self.model.predict(frame)
        if time.monotonic() < self._muted_until:
            return
        name, score = max(scores.items(), key=lambda kv: kv[1])
        if self.debug:
            self._report(name, float(score), frame)
        if score >= self.threshold:
            self._muted_until = time.monotonic() + self.refractory
            self.model.reset()
            self.on_wake(name, float(score))

    def _report(self, name: str, score: float, frame: np.ndarray) -> None:
        """Print the best score and input level once a second, for bring-up."""
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
        level = 20 * np.log10(rms / 32768) if rms > 0 else -99.0
        if score >= self._debug_best[1]:
            self._debug_best = (name, score, level)
        self._debug_n += 1
        if self._debug_n >= SAMPLE_RATE // FRAME:
            best_name, best_score, best_level = self._debug_best
            print(
                f"  level {level:6.1f} dBFS   best {best_name} {best_score:.3f}"
                f" @ {best_level:.1f} dBFS",
                flush=True,
            )
            self._debug_best, self._debug_n = ("", 0.0, 0.0), 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list-devices", action="store_true", help="print input devices and exit")
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        metavar="NAME",
        help="pretrained wake word, repeatable (default: hey_jarvis)",
    )
    parser.add_argument("--device", help="input device name or index")
    parser.add_argument("--rate", type=int, help="capture rate; default is the device's own")
    parser.add_argument("--threshold", type=float, default=0.5, help="detection score, 0-1")
    parser.add_argument(
        "--vad-threshold",
        type=float,
        default=0.0,
        help="Silero VAD gate, 0-1; raise it to cut false positives from noise",
    )
    parser.add_argument("--refractory", type=float, default=2.0, help="seconds muted after a hit")
    parser.add_argument(
        "--webhook",
        help="POST {model, score} here on every hit, e.g. http://localhost:8000/api/wake",
    )
    parser.add_argument(
        "--debug", action="store_true", help="print input level and best score every second"
    )
    args = parser.parse_args(argv)

    if args.list_devices:
        print(list_devices())
        return 0

    device: str | int | None = args.device
    if device is not None and device.isdigit():
        device = int(device)

    def announce(name: str, score: float) -> None:
        print(f"{time.strftime('%H:%M:%S')}  {name}  {score:.3f}", flush=True)
        if not args.webhook:
            return
        # Called from the loop draining the capture queue, not the PortAudio
        # callback, so a slow POST delays scoring rather than dropping samples.
        # Bounded by a timeout inside the refractory window, it needs no thread.
        # The agent being down is not a reason to stop listening for it.
        try:
            request = urllib.request.Request(
                args.webhook,
                data=json.dumps({"model": name, "score": score}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT):
                pass
        # `http.client`'s own errors are not `OSError`s: `HTTPException`
        # derives straight from `Exception`, and `urllib` wraps only what
        # `h.request()` raises, not what `h.getresponse()` does
        # (`urllib/request.py:1317-1326`). So anything listening on the port
        # that answers with something other than a status line raises
        # `BadStatusLine` here, and under the narrower spelling that walks
        # out through `_score` and stops the monitor.
        except (OSError, urllib.error.URLError, http.client.HTTPException) as err:
            print(f"  webhook failed: {err}", flush=True)

    listener = WakeListener(
        announce,
        models=args.models or ["hey_jarvis"],
        threshold=args.threshold,
        vad_threshold=args.vad_threshold,
        refractory=args.refractory,
        device=device,
        capture_rate=args.rate,
        debug=args.debug,
    )
    print(
        f"listening on {sd.query_devices(device, 'input')['name']} "
        f"@ {listener.capture_rate} Hz x{listener.channels}ch "
        f"for {', '.join(listener.model.models)}",
        flush=True,
    )
    try:
        listener.run()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
