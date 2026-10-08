#!/bin/bash
# Record a wake-word take on the Pi, for scoring later with tools/score_wav.py.
#
# A wake word can only be judged against the voice and the room it has to work
# in, and every model and threshold has to be tried against the same take —
# so record once, at the distance you care about, and re-score offline as often
# as you like. Asking a person to speak into a fixed window yields one data
# point and wastes their time.
#
#   ./tools/record_probe.sh ~/gemini3m.wav 60
#   uv run python tools/score_wav.py ~/gemini3m.wav ~/models/hey_gemini.onnx
#
# Say the phrase every ~6 s. score_wav.py collapses peaks within 1.5 s of each
# other and counts activations no closer than 2 s, so utterances any tighter
# than that get counted as one.
set -eu

OUT=${1:-$HOME/probe.wav}
SECS=${2:-60}
CARD=${CARD:-seeed2micvoicec}

# Capture is unusable until the HAT is set up, and it stays that way only until
# the next amixer call anywhere on the system triggers a DAPM sync. Re-run it
# here rather than trusting that boot-time state survived.
"$(dirname "$0")/mic_setup.sh"

echo
echo "Recording ${SECS}s to $OUT — say the phrase every ~6 s, about 8 times."
for i in 3 2 1; do printf '%s... ' "$i"; sleep 1; done
echo "go"

# Stereo: the two capsules are kept separate and averaged at scoring time, which
# is worth ~3 dB over either one alone. 16 kHz because that is what openWakeWord
# takes; anything else and score_wav.py refuses the file rather than resampling
# it behind your back.
arecord -D "hw:$CARD" -f S16_LE -r 16000 -c 2 -d "$SECS" "$OUT"

echo
echo "done: $OUT"
