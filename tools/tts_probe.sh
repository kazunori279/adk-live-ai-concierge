#!/bin/bash
# Speak a wake word through the Mac's speakers while the Pi records, then score
# the take offline. Takes the human out of the bring-up loop: the Mac and the Pi
# sit next to each other, so distance is faked by sweeping the output volume,
# which is the part of distance that actually matters (SNR).
#
#   tools/tts_probe.sh "hey jarvis" hey_jarvis 38 Samantha Daniel Fred
#   tools/tts_probe.sh "hey gemini" ~/models/hey_gemini_fp05.onnx 38
#
# The model argument is whatever score_wav.py takes: a pretrained name, or a
# path to a custom .onnx on the Pi.
#
# The Mac's output volume is restored on exit, including on Ctrl-C.
set -eu

PI=${PI:-pi@192.0.2.10}
PHRASE=${1:?phrase}
MODEL=${2:?openWakeWord model name}
VOLUME=${3:-38}
shift 3
VOICES=("$@")
[ ${#VOICES[@]} -eq 0 ] && VOICES=(Samantha Daniel Fred Kathy Ralph)

GAP=6
DUR=$(( ${#VOICES[@]} * GAP + 6 ))
# A custom model arrives as a path, so strip it down to a bare name before it
# goes anywhere near a filename: /tmp/tts_~/models/x.onnx_v38.wav is a directory
# that does not exist, and arecord's complaint about it went to /dev/null.
TAG=$(basename "$MODEL" .onnx | tr -c 'A-Za-z0-9_.-' '_')
WAV=/tmp/tts_${TAG}_v${VOLUME}.wav

# Capture is unusable until the HAT is set up, and any amixer call anywhere
# undoes the mic bias, so do it here rather than trust that boot-time state
# survived.
ssh "$PI" "~/ai-terminal/tools/mic_setup.sh" >/dev/null

# Check where the Mac is actually playing before reading anything into a low
# score. A take at +2 dB SNR where every model reads 0.001 looks exactly like a
# broken model and is usually the output sitting on a pair of headphones.

WAS=$(osascript -e 'output volume of (get volume settings)')
restore() { osascript -e "set volume output volume $WAS"; }
trap restore EXIT

ssh "$PI" "arecord -D hw:seeed2micvoicec -f S16_LE -r 16000 -c 2 -d $DUR $WAV" \
  >/dev/null &
REC=$!
sleep 2
osascript -e "set volume output volume $VOLUME"
for v in "${VOICES[@]}"; do
  say -v "$v" "$PHRASE"
  sleep $(( GAP - 2 ))
done
wait $REC

ssh "$PI" "cd ~/ai-terminal && UV_LINK_MODE=copy ~/.local/bin/uv run python tools/score_wav.py $WAV $MODEL" 2>&1 |
  grep -v Warn
