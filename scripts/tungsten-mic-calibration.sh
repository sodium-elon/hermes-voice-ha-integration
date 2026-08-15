#!/usr/bin/env bash
# tungsten-mic-calibration.sh — re-apply the calibrated mic gain on Tungsten.
#
# Calibrated 2026-08-15 with scripts/voice_stt_calibration.py --acoustic:
#   source alsa_input.pci-0000_c6_00.6.analog-stereo @ 30% (-31.37 dB)
#   - loud speech (TTS loudness): 19/20 exact, mean conf 0.59, no clipping
#   - at 41%+ peaks exceed 1.0 and clip; at 70% transcripts collapse
#
# WirePlumber normally restores this across reboots; run this if the
# voice monitor starts showing empty or garbage transcripts again.

set -euo pipefail

TARGET=${1:-30%}
HOST=${TUNGSTEN_HOST:-john@192.168.122.1}
SOURCE=alsa_input.pci-0000_c6_00.6.analog-stereo

ssh -o BatchMode=yes "$HOST" "pactl set-source-volume $SOURCE $TARGET && \
    pactl list sources | grep -A12 'Name: $SOURCE' | grep -E '^\s+Volume'"
