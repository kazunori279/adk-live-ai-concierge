"""Measure the impulse response of the path the wake word actually travels.

The MIT set the model trains on is 40 of somebody else's rooms. What the
terminal has to survive is one path: Alex's mouth, across the kitchen, around the
Touch Display 2, to a pair of capsules mounted behind it. Measuring that path
turns "the room" from a guess into a file, which can go straight into the
training RIRs and into rir_bench.py as a pass/fail the deployment cares about.

Farina's exponential sine sweep, because it separates the linear response from
the loudspeaker's harmonic distortion — the distortion products land *before*
the linear impulse in the deconvolved result, so trimming takes them out.

    # 1. on the Mac: make the stimulus, AirDrop sweep.wav to a phone
    uv run python tools/measure_ir.py gen sweep.wav

    # 2. on the Pi: record while the phone plays it, twice
    uv run python tools/measure_ir.py record near.wav   # phone 20 cm from the HAT
    uv run python tools/measure_ir.py record far.wav    # phone where you stand

    # 3. on the Pi: the room, with the phone's own response divided out
    uv run python tools/measure_ir.py deconv far.wav room_ir.wav --ref near.wav

The far take has to be made with the terminal assembled the way it will be
used. Behind the Touch Display 2 the same sweep arrives 15 dB quieter and with
8 dB less DRR than at an exposed HAT, and that gap is the entire reason for
measuring. The near take is unaffected — it characterises the phone, not the
enclosure — so it survives changes to the setup as long as the phone's volume
is untouched between the two.

The near take is also what makes this a measurement of the room rather than of
a phone speaker. Both takes carry the phone's response; dividing one by the
other cancels it and leaves the path. Skip --ref and you bake a 2 cm driver's
frequency response into every training clip. The division takes the HAT's mic
response out along with it, which is the one thing here worth keeping — but a
phone speaker is far less flat than a measurement mic, so it is the right
trade.

`selftest` convolves the sweep with a synthetic IR and checks it comes back.
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import fftconvolve, resample_poly

# The model only ever sees 16 kHz, so there is nothing above 8 kHz worth
# measuring. The stimulus file is 48 kHz because that is what a phone plays
# without resampling; the deconvolution happens at 16 kHz.
FILE_RATE = 48_000
IR_RATE = 16_000
F1, F2 = 80.0, 7_600.0  # 7.6 kHz, safely under the 16 kHz Nyquist
SWEEP_SECONDS = 10.0
LEAD_SILENCE = 1.0
TAIL_SILENCE = 3.0
FADE = 0.03
AMPLITUDE = 0.5  # phone speakers clip well below full scale

# Direct-arrival frequency flatness gate (#508).
# Over a line-of-sight path of a few meters through air, direct sound is physically
# flat across speech frequencies: air absorption is negligible (< 0.1 dB at 8 kHz
# over 2.5 m) and transducer directivity variations stay within +-3 to +-6 dB.
# A deviation exceeding 6 dB (4x power) in the first 5 ms direct window indicates an
# acoustic measurement defect — such as an artifact from near-reference deconvolution
# division — rather than true room propagation.
DIRECT_MAX_DEVIATION_DB = 6.0
DIRECT_WINDOW_MS = 5.0
DIRECT_BANDS: tuple[tuple[str, float, float], ...] = (
    ("250-500", 250.0, 500.0),
    ("500-1k", 500.0, 1000.0),
    ("1-2k", 1000.0, 2000.0),
    ("2-4 kHz", 2000.0, 4000.0),
    ("4-8k", 4000.0, 8000.0),
)


def sweep(rate: int, seconds: float = SWEEP_SECONDS) -> tuple[np.ndarray, np.ndarray]:
    """Return (stimulus, inverse filter) such that their convolution is a spike.

    The inverse is the time-reversed sweep divided by its own exponential
    envelope, which is what flattens the 3 dB/octave tilt an ESS has by
    construction (it spends longer per octave at the bottom).
    """
    n = int(rate * seconds)
    t = np.arange(n) / rate
    r = np.log(F2 / F1)
    x = np.sin(2 * np.pi * F1 * seconds / r * (np.expm1(t * r / seconds)))

    fade = int(rate * FADE)
    ramp = 0.5 * (1 - np.cos(np.pi * np.arange(fade) / fade))
    x[:fade] *= ramp
    x[-fade:] *= ramp[::-1]

    inv = x[::-1] / np.exp(t * r / seconds)
    # Normalise so the recovered impulse has the amplitude of the real path
    # rather than of the stimulus energy.
    inv /= np.abs(fftconvolve(x, inv)).max()
    return x, inv


def read_mono(path: Path, rate: int) -> np.ndarray:
    """Any wav, any rate, any channel count -> mono float at `rate`."""
    sr, d = wavfile.read(str(path))
    scale = 32768.0 if np.issubdtype(d.dtype, np.integer) else 1.0
    d = d.astype(np.float64) / scale
    if d.ndim > 1:
        d = d.mean(1)
    if sr != rate:
        g = np.gcd(sr, rate)
        d = resample_poly(d, rate // g, sr // g)
    return d


def deconvolve(recorded: np.ndarray, tail: float) -> np.ndarray:
    """Recover the impulse response from a recording of the sweep."""
    h, peak = _raw_deconv(recorded)
    # The linear impulse is the largest peak; harmonic distortion sits earlier.
    # A couple of ms of lead keeps the onset, which is the part that carries the
    # direct-to-reverberant ratio the model lives or dies on.
    start = max(0, peak - int(IR_RATE * 0.002))
    return h[start : start + int(IR_RATE * tail)]


def _raw_deconv(recorded: np.ndarray) -> tuple[np.ndarray, int]:
    _, inv = sweep(IR_RATE)
    h = fftconvolve(recorded, inv)
    return h, int(np.argmax(np.abs(h)))


def sweep_snr(recorded: np.ndarray) -> float:
    """Peak of the impulse over the noise floor that precedes it, in dB.

    The check a take has to pass. Level says nothing — a recording made behind
    the display panel peaks 15 dB below one made in the open and is still a
    perfectly good measurement — and neither does crest factor, which cannot
    tell an empty room from a reverberant one. This can: deconvolving a take
    with no sweep in it leaves noise, and noise has no impulse.

    The window ends a full second before the peak, clear of the harmonic
    distortion products an ESS piles up ahead of the linear impulse.
    """
    h, peak = _raw_deconv(recorded)
    pre = h[max(0, peak - int(IR_RATE * 3)) : max(1, peak - IR_RATE)]
    body = h[max(0, peak - 32) : peak + int(IR_RATE * 0.7)]
    floor = float(np.sqrt((pre**2).mean())) or 1e-20
    return 20 * np.log10(np.abs(body).max() / floor)


def window_direct(h: np.ndarray, ms: float) -> np.ndarray:
    """Keep only the direct sound of an impulse response, cosine-faded out.

    Only used on the near-field reference. See divide_out for why.
    """
    end = int(np.argmax(np.abs(h))) + int(IR_RATE * ms / 1000)
    fade = int(IR_RATE * 0.002)
    w = np.zeros_like(h)
    w[:end] = 1.0
    w[max(0, end - fade) : end] = 0.5 * (1 + np.cos(np.pi * np.arange(fade) / fade))
    return h * w


def divide_out(far: np.ndarray, near: np.ndarray, reg: float = 1e-3,
               ref_ms: float = 4.0) -> np.ndarray:
    """far / near in the frequency domain, regularised.

    Both takes contain the phone's speaker response; the quotient does not. The
    regularisation is a water level on the denominator: `near` has almost no
    energy below 100 Hz or above 7.6 kHz, and dividing by that would amplify
    nothing but noise into a result that looks like a cymbal.

    `near` is windowed to its direct sound first, and that is not optional. A
    20 cm take in a live kitchen measured DRR +2.7 dB here — its own reflections
    are almost as loud as the phone — and asking the inverse filter to undo
    those reflections is what the water level cannot rescue. Unwindowed, the
    quotient's energy decay went flat at -17 dB after 0.3 s: a noise floor, not
    a room. RT60 read 1.96 s for a kitchen whose far take says 0.75 s.

    The check that a setting is right: dividing by a 4 ms reference should
    equalise the spectrum and leave the *decay* alone, so the quotient's energy
    decay curve has to track the far take's. Defaults score 0.58 dB RMS
    deviation over the first 0.5 s. Do not tune past that — the grid around it
    is non-monotonic, which means it is scatter and not a slope. 4 ms is chosen
    because that is when the near take's first reflection arrives, not because
    it won a sweep. Its cost is frequency resolution: a 4 ms reference cannot
    describe the phone below ~250 Hz, which is under the speaker's own rolloff.
    """
    near = window_direct(near, ref_ms)
    n = 1 << int(np.ceil(np.log2(len(far) + len(near))))
    F, N = np.fft.rfft(far, n), np.fft.rfft(near, n)
    H = F * np.conj(N) / (np.abs(N) ** 2 + reg * (np.abs(N) ** 2).max())
    h = np.fft.irfft(H, n)[: len(far)]
    return np.roll(h, -int(np.argmax(np.abs(h))) + int(IR_RATE * 0.002))


def direct_arrival(
    h: np.ndarray, max_db: float = DIRECT_MAX_DEVIATION_DB
) -> tuple[bool, float, str]:
    """Test whether the direct arrival's spectrum is flat within max_db.

    Extracts a DIRECT_WINDOW_MS window starting at the peak impulse, computes
    its power spectrum across DIRECT_BANDS, and returns (ok, worst_db, worst_band)
    where worst_db is the maximum deviation across all bands.
    """
    if len(h) == 0:
        return False, float("-inf"), DIRECT_BANDS[0][0]
    peak = int(np.argmax(np.abs(h)))
    w = h[peak : peak + int(IR_RATE * DIRECT_WINDOW_MS / 1000)]
    if len(w) == 0 or np.all(w == 0):
        return False, float("-inf"), DIRECT_BANDS[0][0]
    H = np.fft.rfft(w, 512)
    freqs = np.fft.rfftfreq(512, 1 / IR_RATE)
    devs: list[tuple[float, str]] = []
    for name, f1, f2 in DIRECT_BANDS:
        m = (freqs >= f1) & (freqs < f2)
        pwr = float(np.mean(np.abs(H[m]) ** 2)) if np.any(m) else 0.0
        db = 10.0 * np.log10(pwr) if pwr > 1e-20 else -200.0
        devs.append((db, name))
    worst_db, worst_band = max(devs, key=lambda x: abs(x[0]))
    ok = bool(all(abs(db) <= max_db for db, _ in devs))
    return ok, worst_db, worst_band


def describe(h: np.ndarray) -> str:
    """RT60 by Schroeder backward integration, and the DRR that actually bites."""
    e = np.cumsum(h[::-1] ** 2)[::-1]
    db = 10 * np.log10(e / e.max() + 1e-20)
    a, b = int(np.argmax(db < -5)), int(np.argmax(db < -25))
    rt60 = (b - a) / IR_RATE * 3 if b > a else 0.0
    # Direct sound is conventionally the first 2.5 ms around the peak.
    peak = int(np.argmax(np.abs(h)))
    w = int(IR_RATE * 0.0025)
    direct = float(np.sum(h[max(0, peak - w) : peak + w] ** 2))
    rest = float(np.sum(h**2)) - direct
    drr = 10 * np.log10(direct / rest) if rest > 0 else float("inf")
    ok, dev, band = direct_arrival(h)
    verdict = "pass" if ok else "fail"
    return (
        f"{len(h) / IR_RATE:.2f}s  RT60 {rt60:.2f}s  DRR {drr:+.1f} dB  "
        f"direct {verdict} ({dev:+.1f} dB at {band})"
    )


def cmd_gen(args) -> int:
    x, _ = sweep(FILE_RATE)
    pad = lambda s: np.zeros(int(FILE_RATE * s))  # noqa: E731
    out = np.concatenate([pad(LEAD_SILENCE), x * AMPLITUDE, pad(TAIL_SILENCE)])
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(FILE_RATE)
        w.writeframes((out * 32767).astype(np.int16).tobytes())
    total = len(out) / FILE_RATE
    print(f"{args.out}: {total:.1f}s, {F1:.0f}-{F2:.0f} Hz sweep at {FILE_RATE} Hz")
    return 0


def cmd_record(args) -> int:
    import sounddevice as sd

    device: str | int = int(args.device) if args.device.isdigit() else args.device
    info = sd.query_devices(device, "input")
    rate = int(info["default_samplerate"])
    channels = min(int(info["max_input_channels"]), 2)
    print(f"{info['name']} @ {rate} Hz x{channels}ch for {args.seconds}s — play the sweep now")
    d = sd.rec(int(rate * args.seconds), samplerate=rate, channels=channels, dtype="int16",
               device=device)
    sd.wait()
    with wave.open(args.out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(d.tobytes())
    # Skip the first half second. The TLV320AIC3104 opens a stream with a
    # full-scale pop, and reading the peak across it reports a perfectly healthy
    # level for a recording that is otherwise thirty seconds of silence.
    body = d[int(rate * 0.5) :]
    peak = 20 * np.log10(max(int(np.abs(body).max()), 1) / 32768)
    print(f"{args.out}: peak {peak:.1f} dBFS")
    if peak > -1:
        print("  clipped — turn the phone down and redo")

    # Deconvolve straight away rather than trusting the level.
    rec = read_mono(Path(args.out), IR_RATE)
    snr = sweep_snr(rec)
    print(f"  {describe(deconvolve(rec, 0.4))}  SNR {snr:.1f} dB")
    if snr < 20:
        print("  NO SWEEP FOUND — this take is unusable, play the file and redo")
        return 1
    if snr < 35:
        print("  thin — usable, but turn the phone up and redo both takes if you can")
    return 0


def cmd_deconv(args) -> int:
    rec = read_mono(Path(args.recording), IR_RATE)
    print(f"  recording: SNR {sweep_snr(rec):.1f} dB")
    h = deconvolve(rec, args.tail)
    print(f"  through the path as recorded: {describe(h)}")
    if args.ref:
        near = deconvolve(read_mono(Path(args.ref), IR_RATE), args.tail)
        print(f"  near (phone + direct path): {describe(near)}")
        h = divide_out(h, near, ref_ms=args.ref_ms)
    h = (h / np.abs(h).max() * 0.9).astype(np.float32)
    wavfile.write(args.out, IR_RATE, h)
    print(f"{args.out}: {describe(h)}")
    ok, _, _ = direct_arrival(h)
    return 0 if ok else 1


def cmd_selftest(_args) -> int:
    """Convolve a known IR with the sweep, add noise, and see if it comes back."""
    rng = np.random.default_rng(0)
    truth = np.zeros(int(IR_RATE * 0.4))
    truth[0] = 1.0
    for delay, gain in [(0.008, 0.6), (0.017, -0.45), (0.031, 0.3)]:
        truth[int(IR_RATE * delay)] = gain
    decay = np.exp(-np.arange(len(truth)) / (IR_RATE * 0.06))
    truth += rng.normal(0, 0.05, len(truth)) * decay

    x, _ = sweep(IR_RATE)
    y = fftconvolve(x, truth)
    y += rng.normal(0, 10 ** (-40 / 20) * y.std(), len(y))  # -40 dB SNR
    got = deconvolve(y, 0.4)

    # deconvolve() keeps 2 ms of lead on purpose, so line the two up on their
    # onsets before comparing. Correlation is unforgiving about a shift: the
    # same IR two samples late reads as no match at all.
    got = got[int(np.argmax(np.abs(got))) :]
    n = min(len(got), len(truth))
    corr = float(np.corrcoef(got[:n], truth[:n])[0, 1])
    print(f"recovered vs truth: correlation {corr:.4f}")
    print(f"  truth {describe(truth)}")
    print(f"  got   {describe(got)}")
    # Not 0.99: the synthetic IR has a broadband noise tail and the sweep only
    # excites 80-7600 Hz, so whatever sits outside that band cannot come back.
    # 0.97 is what a correct deconvolution scores here; a broken one scores ~0.
    return 0 if corr > 0.95 else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="write the sweep to play")
    g.add_argument("out", nargs="?", default="sweep.wav")
    g.set_defaults(func=cmd_gen)

    r = sub.add_parser("record", help="record while the sweep plays")
    r.add_argument("out")
    r.add_argument("--device", default="seeed2micvoicec")
    r.add_argument("--seconds", type=float, default=20.0)
    r.set_defaults(func=cmd_record)

    d = sub.add_parser("deconv", help="recording -> impulse response")
    d.add_argument("recording")
    d.add_argument("out")
    d.add_argument("--ref", help="near-field take, to divide out the phone's own response")
    d.add_argument("--ref-ms", type=float, default=4.0,
                   help="ms of direct sound to keep from --ref; raise it and the quotient "
                        "flattens into a noise floor")
    d.add_argument("--tail", type=float, default=0.7, help="seconds of IR to keep")
    d.set_defaults(func=cmd_deconv)

    s = sub.add_parser("selftest", help="check the deconvolution against a known IR")
    s.set_defaults(func=cmd_selftest)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
