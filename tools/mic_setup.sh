#!/bin/bash
# Put the ReSpeaker 2-Mics Pi HAT V2.0 into a state where capture is actually
# usable. The Seeed overlay leaves three things wrong, and each one looks like
# a hardware fault until you know better.
#
# Run after every boot, and after anything else that touches the card: an
# `amixer` call anywhere triggers a DAPM sync, which undoes the mic bias.
set -eu

CARD=${CARD:-seeed2micvoicec}
# 0.5 dB per step. 17 dB leaves ~4 dB of headroom over a talker at 3 m; 20 dB
# put peaks at 0.90 FS, and anything past 27 dB clips on room rumble alone.
PGA=${PGA:-34}

# What the two registers this script touches have to read back. Both used to be
# literals inside the `printf` labels at the bottom, which is the one place a
# value can be wrong and change nothing: mutating the HPF label from 0x50 to
# 0x51 altered this script's output and failed no case anywhere in the repo.
HPF_REG=0x0c;  HPF_WANT=0x50
BIAS_REG=0x19; BIAS_WANT=0x86

C="amixer -c $CARD"

# 1. The ADC high-pass filter is disabled by default, which leaves a DC offset
#    of several thousand counts. It is a *stereo* enum: one value configures
#    the left ADC only and leaves the right at 'Disabled', where its DC offset
#    masquerades perfectly as a broken microphone.
$C sset "ADC HPF Cut-off" "0.0045xFs","0.0045xFs" >/dev/null

$C sset "PGA" "$PGA" >/dev/null
$C sset "Left PGA Mixer Line1L"  on  >/dev/null
$C sset "Left PGA Mixer Line1R"  off >/dev/null
$C sset "Right PGA Mixer Line1L" off >/dev/null
$C sset "Right PGA Mixer Line1R" on  >/dev/null
$C sset "Left Line1L Mux"  single-ended >/dev/null
$C sset "Right Line1R Mux" single-ended >/dev/null

# 2. The overlay's `simple-audio-card,routing` never names "Mic Bias", so DAPM
#    sees an isolated widget and powers it down on every sync. Forced straight
#    into the codec because there is no mixer control for it. The proper fix is
#    a route in the overlay and a rebuilt dtbo.
sudo i2cset -f -y 1 0x18 "$BIAS_REG" "$BIAS_WANT"

# 3. Register 0x0C only reads back 0x50 while a stream is open; the driver's
#    own cache will happily claim the filter is on when the register says 0x00.
#    So check it from inside a capture, not before one.
arecord -D "hw:$CARD" -f S16_LE -r 16000 -c 2 -d 1 /dev/null 2>/dev/null &
sleep 0.4
#    Read into variables first, and not inline in the printf: a command
#    substitution in an argument is not the command `set -e` looks at, so a
#    failing i2cget there prints an empty value and leaves the script exiting
#    zero. As its own assignment the same failure aborts.
hpf=$(sudo i2cget -f -y 1 0x18 "$HPF_REG")
bias=$(sudo i2cget -f -y 1 0x18 "$BIAS_REG")
printf 'HPF  reg 0x0C = %s (want %s)\n' "$hpf" "$HPF_WANT"
printf 'BIAS reg 0x19 = %s (want %s)\n' "$bias" "$BIAS_WANT"
wait

# 4. And act on the high-pass reading rather than printing it. Anything but
#    0x50 is either the filter off — the DC offset in note 1, which reads as a
#    dead microphone — or the capture on the line above never opening, in which
#    case this run has not shown the card is usable either. Both are the step
#    failing. `pi_deploy.run_steps` reads a non-zero exit before it consults
#    the step's verify, so the exit status is enough and no caller has to learn
#    a new output format.
if [ "$hpf" != "$HPF_WANT" ]; then
    printf 'the ADC high-pass filter did not take: %s reads %s, want %s\n' \
        "$HPF_REG" "$hpf" "$HPF_WANT" >&2
    exit 1
fi
