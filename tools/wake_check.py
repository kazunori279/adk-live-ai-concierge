"""Check what the wake monitor does with the audio it is handed.

`deploy_check.py` lifts `wake.main`'s parser out and hands it the unit's
`ExecStart`, so the flags are watched. Nothing watches what happens after the
flags are parsed. The monitor's whole job is four small decisions taken a
hundred and eighty thousand times an hour — downmix, resample, warm up, score
against a number — and every one of them fails the same way: the terminal
stays quiet and the journal says nothing, because a monitor that hears nothing
and a monitor that is working in a silent room write the same log.

Three of those decisions are one comparison each, which is the shape of defect
that survives a suite that never runs the code:

- **The threshold is the score to fire *at*.** `--threshold 0.2` is a number
  three rooms were measured to settle on, and `>=` weakened to `>` moves the
  monitor off that measurement by one float without changing a flag, a log
  line or the README.
- **The first frames of a stream are a codec artefact, not a word.** The
  TLV320AIC3104 opens with a full-scale step; scoring it primes the model with
  a transient.
- **The mute ends when it says it ends.** It is what turns one utterance into
  one callback, and it is one `<` against a monotonic clock.

None of that needs a microphone, and none of it is checked here with one.
`wake` reaches audio through exactly one name — the module global `sd` — and
this script replaces that name, the model behind it and the clock beside it.
The real `sounddevice`'s stream constructors are replaced with tripwires at
import, and the last case reads them back: if anything in this file had
reached a card, the run is red whether or not the case that did it noticed.
The model is faked for the same reason and one more, which is that
`ensure_models` downloads the ONNX files over the network on first use.

What stays a device claim, and is written down in the README rather than
here: that the HAT captures at the rate it reports, that `resample_poly` on
80 ms blocks costs less than the frame period on a Pi 5, and that the phrase
said in the kitchen scores above 0.2. A fake for any of those is green on a
laptop with no microphone, which is worse than the gap it fills.

    uv run python tools/wake_check.py

Exits non-zero on the first case that does not hold.
"""

import contextlib
import http.client
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import numpy as np
import sounddevice
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ai_terminal import wake  # noqa: E402

RAN = 0

# Every name on the real `sounddevice` that opens or reads a card. Replaced
# before the first fixture is built, so a fixture that forgot to swap `wake.sd`
# raises here instead of turning the laptop's microphone on. `REACHED` is read
# back by the last case, because `check` exits on the first failure and a
# tripwire that fired inside a case that passed anyway would otherwise be lost.
REACHED: list[str] = []


def _tripwire(name: str):
    def fired(*_args, **_kwargs):
        REACHED.append(name)
        raise AssertionError(f"this check reached the real sounddevice.{name}")
    return fired


for _name in ("InputStream", "RawInputStream", "Stream", "RawStream",
              "rec", "query_devices"):
    setattr(sounddevice, _name, _tripwire(_name))


def check(name: str, got, want, detail: str = "") -> None:
    global RAN
    RAN += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail or got}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


class Clock:
    """`time`, as `wake` reads it.

    Two calls: `monotonic` for the mute window and `strftime` for the line the
    announce prints. A real clock cannot be stopped on the instant the mute
    ends, and that instant is the whole of what `<` means here.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def strftime(self, _fmt: str) -> str:
        return "07:41:03"


CLOCK = Clock()
wake.time = CLOCK


class Tape:
    """A capture stream that opens, closes, and touches no card."""

    def __init__(self, rig: "Rig | None" = None) -> None:
        self.rig = rig
        self.entered = 0
        self.closed = 0

    def __enter__(self):
        self.entered += 1
        if self.rig is not None and self.rig._pending_reel is not None:
            if isinstance(self.rig.listener._queue, Reel):
                self.rig.listener._queue.blocks.extend(self.rig._pending_reel.blocks)
            else:
                self.rig.listener._queue = self.rig._pending_reel
            self.rig._pending_reel = None
        return self

    def __exit__(self, *_exc) -> bool:
        self.closed += 1
        return False


class Card:
    """`sounddevice`, reduced to the two calls `wake` makes of it.

    The device record is the shape `query_devices` returns for an input: the
    two fields read out of it are the rate and the channel count, and the name
    is only ever printed.
    """

    def __init__(self, rate: float = 48000.0, channels: int = 2) -> None:
        self.info = {"name": "fake", "default_samplerate": rate,
                     "max_input_channels": channels}
        self.opened: list[dict] = []
        self.stream: Tape | None = None
        self.rig: Rig | None = None

    def query_devices(self, _device=None, _kind=None) -> dict:
        return self.info

    def InputStream(self, **kwargs) -> Tape:  # noqa: N802 - sounddevice's name
        self.opened.append(kwargs)
        self.stream = Tape(self.rig)
        return self.stream


class Ear:
    """openWakeWord's `Model`, reduced to the three things `_score` uses.

    Scores are read off a list in the order the frames arrive, so a case says
    what the room said rather than trying to synthesise audio that a real
    model would score above 0.2. A queued entry may be a dict, which is how
    the two-model case gets two scores out of one frame.
    """

    def __init__(self, said=()) -> None:
        self.built: dict = {}
        self.said = list(said)
        self.heard: list[np.ndarray] = []
        self.resets = 0
        self.models = {"hey_jarvis": None}

    def take(self, built: dict) -> "Ear":
        self.built = built
        return self

    def predict(self, frame: np.ndarray) -> dict:
        self.heard.append(frame)
        said = self.said.pop(0) if self.said else 0.0
        return said if isinstance(said, dict) else {"hey_jarvis": said}

    def reset(self) -> None:
        self.resets += 1


class Ended(Exception):
    """The end of the tape, which is how `run` is stopped.

    `run` has no exit condition — on the Pi the unit is what stops it — so a
    fixture ends the capture loop the only way there is, from the queue it
    blocks on.
    """


class Reel:
    """The capture queue, pre-filled, in place of the callback's own."""

    def __init__(self, blocks) -> None:
        self.blocks = list(blocks)

    def get(self) -> np.ndarray:
        if not self.blocks:
            raise Ended
        return self.blocks.pop(0)


class Rig:
    """A `WakeListener` with the card, the model and the clock all faked."""

    def __init__(self, said=(), *, rate=48000.0, channels=2, threshold=0.5,
                 refractory=2.0, capture_rate=None, debug=False,
                 models=("hey_jarvis",)) -> None:
        self.card = Card(rate, channels)
        self.card.rig = self
        self.ear = Ear(said)
        self.hits: list[tuple[str, float]] = []
        self._pending_reel: Reel | None = None
        wake.sd = self.card
        wake.Model = lambda **built: self.ear.take(built)
        wake.ensure_models = lambda: None
        self.listener = wake.WakeListener(
            lambda name, score: self.hits.append((name, score)),
            models=models, threshold=threshold, refractory=refractory,
            capture_rate=capture_rate, debug=debug,
        )

    def play(self, blocks) -> None:
        """Run the capture loop over `blocks` until the tape runs out."""
        self._pending_reel = Reel(blocks)
        with contextlib.suppress(Ended):
            self.listener.run()

    def warm(self) -> "Rig":
        """Spend the warmup, for the cases that call `_score` directly.

        Not for the ones that go through `play`: `run` arms the warmup for
        the stream it opens, so a tape spends it in silence instead (see
        `lead` in `pending_cases`).
        """
        self.listener._warmup = 0
        return self


def frame(value: int = 0) -> np.ndarray:
    """One 80 ms frame at 16 kHz, which is what `_score` takes."""
    return np.full(wake.FRAME, value, dtype=np.int16)


def blocks(samples: int, channels: int = 1) -> np.ndarray:
    """One capture block, in the shape sounddevice hands the callback."""
    return np.zeros((samples, channels), dtype=np.int16)


def refused(channels: int) -> str:
    """What building a listener on that card says, or nothing if it built."""
    try:
        Rig(channels=channels)
    except RuntimeError as err:
        return str(err)
    return ""


def started(argv: list[str]) -> tuple[int, dict]:
    """`main`, with the listener it builds replaced by one that stops.

    The callback `main` writes is a closure, so the only way to the announce
    is through the constructor it is handed to. The stub raises the exception
    a person at the terminal raises, which is also how `main` is meant to end.
    """
    held: dict = {}
    wake.sd = Card()

    class Stub:
        def __init__(self, on_wake, **kwargs) -> None:
            held["on_wake"] = on_wake
            held["kwargs"] = kwargs
            self.capture_rate = 48000
            self.channels = 2
            self.model = Ear()

        def run(self) -> None:
            raise KeyboardInterrupt

    real, wake.WakeListener = wake.WakeListener, Stub
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            code = wake.main(argv)
    finally:
        wake.WakeListener = real
    return code, held


def posted(name: str, score: float,
           webhook: str | None = "http://127.0.0.1:8000/api/wake",
           fails: Exception | None = None) -> tuple[list[dict], str]:
    """What the announce sends, with the socket taken out from under it.

    Only `urlopen` is replaced, and only around the one call: the `Request`
    that reaches it is the real one, so the URL, the body and the header read
    here are the bytes the Pi would have put on the wire. The URL is loopback
    as well, so a fake that failed to take would still reach nothing.
    """
    sent: list[dict] = []

    class Answered:
        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> bool:
            return False

    def fake(request, timeout=None):
        sent.append({"url": request.full_url,
                     "body": json.loads(request.data.decode()),
                     "type": request.get_header("Content-type"),
                     "timeout": timeout})
        if fails is not None:
            raise fails
        return Answered()

    argv = ["--webhook", webhook] if webhook else []
    _code, held = started(argv)
    real, urllib.request.urlopen = urllib.request.urlopen, fake
    said = io.StringIO()
    try:
        with contextlib.redirect_stdout(said):
            held["on_wake"](name, score)
    finally:
        urllib.request.urlopen = real
    return sent, said.getvalue()


def downmix_cases() -> None:
    print("== the capture block, on its way to 16 kHz mono ==")
    stereo = np.array([[0, 10], [2, 12], [4, 14]], dtype=np.int16)
    # Not the left channel and not the sum: the HAT's two capsules are 20 mm
    # apart, so both carry the word and one of them alone is 3 dB of the room.
    check("the two capsules are averaged, not one of them taken",
          list(wake._downmix_resample(stereo, wake.SAMPLE_RATE)), [5, 7, 9])
    mono = np.array([-3, 0, 3], dtype=np.int16)
    check("a block with no second axis has nothing to average",
          list(wake._downmix_resample(mono, wake.SAMPLE_RATE)), [-3, 0, 3])
    # A 16 kHz card exists — `--rate 16000` is in the README — and its block
    # has to come out sample-identical rather than merely close. scipy would
    # short-circuit `up == down == 1` anyway (`_signaltools.py:4028`), so the
    # `rate != SAMPLE_RATE` guard costs nothing; this pins the result, not it.
    long_mono = np.arange(-500, 500, dtype=np.int16)
    check("and a block already at 16 kHz comes out sample for sample",
          list(wake._downmix_resample(long_mono, wake.SAMPLE_RATE)),
          list(long_mono), f"{len(long_mono)} samples unchanged")

    at_48k = blocks(wake.FRAME * 3, 2)
    check("48 kHz comes back a third as long, which is one frame",
          len(wake._downmix_resample(at_48k, 48000)), wake.FRAME)
    # 44 100 is not a whole multiple of 16 000, and 80 ms of it is 3528
    # samples, so this is the case that says the ratio is done in the
    # resampler rather than by dropping samples.
    check("and 44.1 kHz, which is not a whole multiple, lands on it too",
          len(wake._downmix_resample(blocks(3528, 2), 44100)), wake.FRAME)
    check("the frame handed to the model is int16, whatever arrived",
          wake._downmix_resample(blocks(3840, 2), 48000).dtype,
          np.dtype(np.int16))

    # A square wave is the worst case for a polyphase filter and the codec
    # emits one on the first block of every stream. The overshoot below is
    # measured, not assumed: without the clip, `astype(np.int16)` on 38 130 is
    # not a loud sample, it is a sample of the opposite sign.
    step = np.where((np.arange(3840) // 24) % 2, 32767, -32768)
    square = np.stack([step, step], axis=1).astype(np.int16)
    ringing = resample_poly(square.mean(axis=1), wake.SAMPLE_RATE, 48000)
    check("a resampled square wave really does ring past full scale",
          int(max(ringing.max(), -ringing.min())) > 32767, True,
          f"{ringing.max():.0f}")
    clipped = wake._downmix_resample(square, 48000)
    check("so the overshoot is clipped rather than wrapped round",
          [int(clipped.max()), int(clipped.min())], [32767, -32768])


def stream_cases() -> None:
    print("\n== the stream the monitor asks the card for ==")
    check("the capture rate is the card's own when --rate is not given",
          Rig(rate=44100.0).listener.capture_rate, 44100)
    check("and the rate asked for when it is, since the HAT lies about its",
          Rig(rate=44100.0, capture_rate=16000).listener.capture_rate, 16000)
    # PipeWire's default source reports whatever the graph is wired for, and
    # a wake word does not get better past two capsules.
    check("no more than two channels are opened, however many are offered",
          Rig(channels=6).listener.channels, 2)
    check("a card with no capture channels is refused, not opened",
          refused(0), "input device has no capture channels: fake")

    rig = Rig(rate=48000.0).warm()
    rig.play([])
    opened = rig.card.opened[0]
    # 80 ms at the capture rate is one frame once converted, so the resampler
    # is handed whole frames and `_pending` stays empty on a well-behaved card.
    check("the block is 80 ms of capture-rate audio", opened["blocksize"],
          3840)
    check("the card is opened at its own rate, in the width it captures",
          [opened["samplerate"], opened["dtype"]], [48000, "int16"])
    check("with the channel count the listener settled on",
          opened["channels"], 2)
    check("and the listener's own callback, not a lambda over it",
          opened["callback"], rig.listener._callback, "_callback")
    check("the stream is entered and closed rather than left open",
          [rig.card.stream.entered, rig.card.stream.closed], [1, 1])

    at_16k = Rig(rate=16000.0, channels=1).warm()
    at_16k.play([])
    check("at 16 kHz the block is the frame size itself",
          at_16k.card.opened[0]["blocksize"], wake.FRAME)
    at_44k = Rig(rate=44100.0).warm()
    at_44k.play([])
    check("and at 44.1 kHz it is still 80 ms, to the sample",
          at_44k.card.opened[0]["blocksize"], 3528)


def pending_cases() -> None:
    print("\n== frames, and what is left over between blocks ==")
    # `play` is a whole `run`, so consecutive blocks are one tape and not two
    # calls: a second call is a second stream, which arms a second warmup.
    # `lead` is that warmup, spent as silence at the head of every tape here.
    lead = blocks(wake.FRAME * wake.WARMUP_FRAMES)
    rig = Rig([0.0] * 8, rate=16000.0, channels=1)
    rig.play([lead, blocks(wake.FRAME + 300)])
    check("a block that is not a whole number of frames scores what it can",
          len(rig.ear.heard), 1)
    # Dropping the remainder would lose 300 samples out of every block, which
    # is a word cut into pieces rather than a word that scores low.
    check("and holds the remainder over instead of dropping it",
          len(rig.listener._pending), 300)
    whole = Rig([0.0] * 8, rate=16000.0, channels=1)
    whole.play([lead, blocks(wake.FRAME + 300), blocks(wake.FRAME - 300)])
    check("which the next block completes into a whole frame",
          [len(whole.ear.heard), len(whole.listener._pending)], [2, 0])
    check("every frame is the length the model was trained on",
          sorted({len(f) for f in whole.ear.heard}), [wake.FRAME])


def warmup_cases() -> None:
    print("\n== the pop at the start of a stream, which is not a word ==")
    spent = wake.WARMUP_FRAMES
    # The block below is sized off the constant, so a warmup of zero frames
    # would drop zero frames and read as green. Both ends of the window are
    # decisions: the unit is `Restart=always`, so this is how long the monitor
    # is deaf after every restart, and it has to cover the codec's step.
    seconds = spent * wake.FRAME / wake.SAMPLE_RATE
    check("the window is a real one, and short enough to restart into",
          [spent > 0, seconds < 2.0], [True, True],
          f"{spent} frames, {seconds:.2f} s")
    rig = Rig([0.99] * 8, rate=16000.0, channels=1)
    # Split across two blocks, because a warmup reset per block would be spent
    # forever on a card that delivers one frame at a time.
    rig.play([blocks(wake.FRAME * (spent // 2)),
              blocks(wake.FRAME * (spent - spent // 2))])
    check("the opening frames of a stream never reach the model",
          len(rig.ear.heard), 0, f"{spent} frames dropped")
    check("so the codec's full-scale step cannot be heard as a word",
          rig.hits, [])
    # One tape and three blocks, not a second `play`: `play` is a whole `run`,
    # and a second `run` is a second stream rather than more of this one.
    spans = Rig([0.99] * 8, rate=16000.0, channels=1)
    spans.play([blocks(wake.FRAME * (spent // 2)),
                blocks(wake.FRAME * (spent - spent // 2)),
                blocks(wake.FRAME)])
    check("the frame after them is scored", len(spans.ear.heard), 1)
    check("and the warmup is spent once, not once per block",
          len(spans.hits), 1)

    # A stream is not the process. The PipeWire graph restarting, the HAT
    # re-enumerating, anything holding the listener calling `run` again: each
    # of those opens a stream, and the TLV320AIC3104 steps into every one of
    # them. A warmup spent on the stream that has gone is no warmup at all.
    again = Rig([0.99] * 8, rate=16000.0, channels=1)
    again.play([blocks(wake.FRAME * spent)])
    again.play([blocks(wake.FRAME * spent)])
    check("the step at the head of a re-opened stream is no more a word",
          again.hits, [], f"{len(again.card.opened)} streams opened")
    # Both ends of the re-armed window, as for the first one: the step is
    # dropped and the frame past it is not, so a monitor that has just got its
    # card back is listening again rather than deaf until the process dies.
    after = Rig([0.99] * 8, rate=16000.0, channels=1)
    after.play([blocks(wake.FRAME * spent)])
    after.play([blocks(wake.FRAME * (spent + 1))])
    check("its opening frames being dropped as the first stream's were",
          [len(again.ear.heard), len(after.ear.heard)], [0, 1],
          f"{spent} dropped, frame {spent + 1} scored")

    # An aborted stream may leave capture blocks in `_queue` and audio in
    # `_pending`. When `run` is re-entered, that stale audio must be discarded
    # so the new stream's warmup window is spent on the new stream's opening
    # step, rather than letting the step leak past warmup into scoring.
    class Abort(Exception):
        pass

    unwound = Rig([0.99] * 8, rate=16000.0, channels=1)
    frames_seen = 0
    real_score = unwound.listener._score

    def abort_on_third(f):
        nonlocal frames_seen
        frames_seen += 1
        if frames_seen == 3:
            unwound.listener._score = real_score
            raise Abort
        real_score(f)

    unwound.listener._score = abort_on_third
    with contextlib.suppress(Abort):
        unwound.play([blocks(wake.FRAME * 6), blocks(wake.FRAME)])

    check("a mid-drain abort leaves pending audio and blocks queued",
          [len(unwound.listener._pending), len(unwound.listener._queue.blocks)],
          [3840, 1])
    unwound.play([blocks(wake.FRAME * spent)])
    check("re-entering run drains leftover queue so warmup protects the new stream",
          [len(unwound.ear.heard), unwound.hits], [0, []])


def score_cases() -> None:
    print("\n== the score, against the number the rooms settled on ==")
    over = Rig([0.9], threshold=0.5).warm()
    over.listener._score(frame())
    check("a score above the threshold is a hit", over.hits,
          [("hey_jarvis", 0.9)])
    under = Rig([0.49], threshold=0.5).warm()
    under.listener._score(frame())
    check("and one below it is not", under.hits, [])
    # `--threshold 0.2` is the number three room measurements settled on. It
    # is the score to fire at, not the score to fire above, and the difference
    # between those two readings is invisible in every log the Pi writes.
    on = Rig([0.5], threshold=0.5).warm()
    on.listener._score(frame())
    check("a score exactly on the threshold is a hit, not a near miss",
          on.hits, [("hey_jarvis", 0.5)])

    both = Rig([{"hey_jarvis": 0.1, "hey_gemini": 0.8}], threshold=0.5).warm()
    both.listener._score(frame())
    check("with two models loaded, the louder one is what gets compared",
          both.hits, [("hey_gemini", 0.8)])
    check("and the callback is told which of them it was",
          both.hits[0][0], "hey_gemini")
    # Without the reset the tail of the same utterance is still in the feature
    # buffer when the mute lifts, and scores again off audio already spent.
    check("a hit clears the model's buffers, so its own tail is not re-scored",
          both.ear.resets, 1)
    check("a frame that is not a hit leaves them alone", under.ear.resets, 0)


def mute_cases() -> None:
    print("\n== the mute that turns one utterance into one callback ==")
    rig = Rig([0.9] * 8, threshold=0.5, refractory=2.0).warm()
    CLOCK.now = 1000.0
    rig.listener._score(frame())
    check("a hit mutes scoring for the window the flag asks for",
          rig.listener._muted_until, 1002.0)
    ends = rig.listener._muted_until
    rig.listener._score(frame())
    check("so the second frame of one spoken word fires nothing",
          len(rig.hits), 1)
    # Muted is not deaf. openWakeWord scores off a rolling feature buffer, and
    # a model starved for two seconds has nothing to say for a second after.
    check("but the frames still reach the model, so its buffer stays fed",
          len(rig.ear.heard), 2)

    CLOCK.now = ends - 1e-6
    rig.listener._score(frame())
    check("a hair before the window ends it is still muted", len(rig.hits), 1)
    CLOCK.now = ends
    rig.listener._score(frame())
    check("and at the instant it ends, scoring is back", len(rig.hits), 2)
    check("with the next window measured from there",
          rig.listener._muted_until, ends + 2.0)

    longer = Rig([0.9], threshold=0.5, refractory=7.5).warm()
    CLOCK.now = 500.0
    longer.listener._score(frame())
    check("the window is the one --refractory names, not a constant",
          longer.listener._muted_until, 507.5)


def announce_cases() -> None:
    print("\n== the webhook, and the agent at the other end being down ==")
    sent, _said = posted("hey_gemini", 0.873)
    # The POST is made on the thread draining the capture queue, so its
    # timeout is time the monitor is not scoring. Inside the mute it costs
    # nothing, because nothing would have been scored anyway.
    check("bounded by a timeout no longer than the mute it fires inside",
          [sent[0]["timeout"],
           wake.WEBHOOK_TIMEOUT <= started([])[1]["kwargs"]["refractory"]],
          [wake.WEBHOOK_TIMEOUT, True], f"{wake.WEBHOOK_TIMEOUT}s")

    # And the failure that timeout produces, which is why the `except` names
    # `OSError` and not only `URLError`. A refused connection arrives wrapped,
    # and since `URLError` is an `OSError` subclass, raising one cannot tell
    # the two spellings apart. An agent that accepts the connection and then
    # says nothing is what separates them: `urlopen` raises `TimeoutError`
    # bare at `WEBHOOK_TIMEOUT`, and under the narrower spelling that one
    # escapes the callback onto the thread draining the capture queue.
    try:
        hung, said = posted("hey_gemini", 0.5, fails=TimeoutError("timed out"))
    except TimeoutError as err:
        hung, said = [], f"left the callback: {err!r}"
    check("an agent that hangs does not take the monitor down with it",
          [len(hung), "webhook failed" in said], [1, True],
          said.strip().splitlines()[-1])

    # A `TimeoutError` is an `OSError`, which is why the case above holds.
    # `http.client`'s own errors are not: `HTTPException` derives straight
    # from `Exception`, and `urllib` wraps only what `h.request()` raises, not
    # what `h.getresponse()` does (`urllib/request.py:1317-1326`). Both of the
    # two below came off a real loopback socket first — one answering the POST
    # with a line that is not a status line, one with a header that never
    # ends — and neither is an `OSError`, so `OSError` does not cover them.
    try:
        junk, said = posted("hey_gemini", 0.5,
                            fails=http.client.BadStatusLine("PORT 8000 BUSY"))
    except http.client.HTTPException as err:
        junk, said = [], f"left the callback: {err!r}"
    check("nor does something on the port that answers but is not the agent",
          [len(junk), "webhook failed" in said], [1, True],
          said.strip().splitlines()[-1])
    try:
        cut, said = posted("hey_gemini", 0.5,
                           fails=http.client.LineTooLong("header line"))
    except http.client.HTTPException as err:
        cut, said = [], f"left the callback: {err!r}"
    check("and the family is named, not the one member of it that was found",
          [len(cut), "webhook failed" in said], [1, True],
          said.strip().splitlines()[-1])

    # The other edge of that tuple. `except Exception` passes every case above
    # and costs the fault its voice: a typo inside the `try` would print
    # `webhook failed` on every wake and the monitor would go on scoring and
    # never announce anything. Three named transport families is the whole
    # point, so a fault that is not the transport's still leaves the callback.
    try:
        typo, said = posted("hey_gemini", 0.5,
                            fails=AttributeError("no attribute 'full_url'"))
    except AttributeError:
        typo, said = [], "left the callback"
    check("but a bug in the callback is not dressed up as a webhook failure",
          [len(typo), said], [0, "left the callback"])


# What `ORT_DISABLE_TELEMETRY` held at the instant openWakeWord was first
# looked up, which is the only instant that decides anything: onnxruntime reads
# the variable once, as its extension loads, and nothing said afterwards moves
# the client. The finder raises rather than returning None, so the import stops
# one step short of `openwakeword/vad.py:48` and the telemetry client this is
# about is never started by the case that checks for it.
PROBE = """
import importlib.util, json, os, sys


class Stop(Exception):
    pass


class Watch:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == "openwakeword":
            raise Stop(os.environ.get("ORT_DISABLE_TELEMETRY"))
        return None


sys.meta_path.insert(0, Watch())
spec = importlib.util.spec_from_file_location("wake_under_test", sys.argv[1])
try:
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
except Stop as stopped:
    print(json.dumps({"held": stopped.args[0]}))
else:
    print(json.dumps({"held": "openWakeWord was never reached"}))
"""


def sampled(source: Path, **room: str):
    """Run `source` under the probe and hand back what it sampled."""
    env = {k: v for k, v in os.environ.items() if k != "ORT_DISABLE_TELEMETRY"}
    env.update(room)
    done = subprocess.run([sys.executable, "-c", PROBE, str(source)],
                          capture_output=True, text=True, env=env)
    if done.returncode != 0:
        return done.stderr.strip().splitlines()[-1:]
    return json.loads(done.stdout.strip().splitlines()[-1])["held"]


def telemetry_cases() -> None:
    print("\n== the telemetry client that arrives with openWakeWord ==")
    source = ROOT / "src" / "ai_terminal" / "wake.py"
    check("onnxruntime is told to stay quiet before openWakeWord is looked up",
          sampled(source), "1")

    # Run rather than argued, and the reason the case above is worth anything:
    # with the guard deleted the variable is unset at the moment that decides,
    # and the same probe says so. A machine that already had the variable in
    # its environment would otherwise make the first case green by itself.
    stripped = re.sub(r"(?m)^os\.environ.*ORT_DISABLE_TELEMETRY.*\n", "",
                      source.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        mutant = Path(tmp) / "wake.py"
        mutant.write_text(stripped, encoding="utf-8")
        check("and with that one line taken back out, nothing is",
              sampled(mutant), None)

    # `setdefault` and not an assignment, so somebody who wants the telemetry
    # keeps it. It is also how the guard comes off by accident: onnxruntime
    # reads the value, and `0` is not a value that disables anything.
    check("a setting already in the environment is left alone, 0 included",
          sampled(source, ORT_DISABLE_TELEMETRY="0"), "0")

    # The guard only guards what comes through this module, so a check script
    # that imported openWakeWord itself would start the client before `wake`
    # was ever reached. The scan is over the scripts the sweep runs:
    swept = sorted(ROOT.glob("tools/*_check.py"))
    direct = sorted(p.name for p in swept
                    if re.search(r"(?m)^\s*(?:import|from)\s+"
                                 r"(?:openwakeword|onnxruntime)\b",
                                 p.read_text(encoding="utf-8")))
    check("and no script in the sweep reaches openWakeWord any other way",
          direct, [], f"{len(swept)} scripts read")

    # Nor through a tool the sweep imports: `deploy_check.py` imports
    # `pi_deploy.py` before `wake`, which would start the telemetry client
    # before `wake.py:54` set the environment variable. The rest of `tools/`
    # outside the sweep is scanned except for the three standalone bench
    # scripts that intentionally run openWakeWord directly.
    bench = {"live_score.py", "rir_bench.py", "score_wav.py", "noise_budget.py"}
    helpers = sorted(p for p in ROOT.glob("tools/*.py")
                     if p.name not in bench and p not in swept)
    indirect = sorted(p.name for p in helpers
                      if re.search(r"(?m)^\s*(?:import|from)\s+"
                                   r"(?:openwakeword|onnxruntime)\b",
                                   p.read_text(encoding="utf-8")))
    check("nor does anything a sweep script imports",
          indirect, [], f"{len(helpers)} scripts read")


def noise_budget_cases() -> None:
    print("\n== noise budget tool ==")
    sys.path.insert(0, str(ROOT / "tools"))
    from noise_budget import build_parser, mix_at_snr, suppress_noise

    sr = 16000
    t = np.linspace(0, 1.0, sr, endpoint=False)
    speech = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    rng = np.random.default_rng(42)
    noise = rng.standard_normal(sr).astype(np.float32)

    # Test mix_at_snr scales noise to exact requested SNR (10 dB, 0 dB, -5 dB)
    for target_snr in (10.0, 0.0, -5.0):
        s_out, n_out = mix_at_snr(speech, noise, target_snr)
        p_s = float(np.mean(s_out**2))
        p_n = float(np.mean(n_out**2))
        measured_snr = 10.0 * np.log10(p_s / p_n)
        check(f"mix_at_snr scales noise to {target_snr:+.0f} dB",
              round(measured_snr, 4), round(target_snr, 4))

    # Test repeat and trim on mismatched lengths
    short_noise = noise[: sr // 2]
    _, n_short = mix_at_snr(speech, short_noise, 0.0)
    check("mix_at_snr repeats noise when shorter than speech",
          len(n_short), len(speech))

    long_noise = np.tile(noise, 2)
    _, n_long = mix_at_snr(speech, long_noise, 0.0)
    check("mix_at_snr trims noise when longer than speech",
          len(n_long), len(speech))

    # Test suppress_noise attenuates signal power by exactly requested dB
    test_noise = rng.standard_normal(sr).astype(np.float32)
    p_orig = float(np.mean(test_noise**2))
    for db in (0.0, 3.0, 6.0, 12.0, 20.0):
        atten = suppress_noise(test_noise, db)
        p_atten = float(np.mean(atten**2))
        measured_db = 10.0 * np.log10(p_orig / p_atten) if p_atten > 0 else 999.0
        check(f"suppress_noise attenuates power by {db:.0f} dB",
              round(measured_db, 4), round(db, 4))

    # Test argument parser handling with valid arguments and defaults
    parser = build_parser()
    args_default = parser.parse_args(["clips_dir", "model.onnx"])
    check("noise_budget parser defaults: clips", args_default.clips, "clips_dir")
    check("noise_budget parser defaults: models", args_default.models, ["model.onnx"])
    check("noise_budget parser defaults: noise", args_default.noise, None)
    check("noise_budget parser defaults: snr", args_default.snr, [-5.0, 0.0, 5.0, 10.0, 15.0])
    check("noise_budget parser defaults: db", args_default.db, [0.0, 3.0, 6.0, 9.0, 12.0, 15.0, 20.0])

    args_custom = parser.parse_args([
        "clips_dir", "m1.onnx", "m2.onnx",
        "--noise", "noise.wav",
        "--snr", "-10.0", "0.0", "10.0",
        "--db", "0.0", "6.0", "12.0",
    ])
    check("noise_budget parser custom args",
          (args_custom.models, args_custom.noise, args_custom.snr, args_custom.db),
          (["m1.onnx", "m2.onnx"], "noise.wav", [-10.0, 0.0, 10.0], [0.0, 6.0, 12.0]))


def isolation_cases() -> None:
    print("\n== what this run was not allowed to touch ==")
    check("`wake` reaches audio through one module global and no other",
          [isinstance(wake.sd, Card), wake.sd is sounddevice], [True, False])
    # Read back rather than trusted: `check` exits on the first failure, so a
    # tripwire that fired inside a case whose assertion passed anyway would
    # otherwise leave no mark on the run.
    check("and no real capture device was opened anywhere in it", REACHED, [])


def main() -> None:
    downmix_cases()
    stream_cases()
    pending_cases()
    warmup_cases()
    score_cases()
    mute_cases()
    announce_cases()
    telemetry_cases()
    noise_budget_cases()
    isolation_cases()
    print(f"\nall {RAN} cases hold")


if __name__ == "__main__":
    main()
