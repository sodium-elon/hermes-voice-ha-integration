#!/usr/bin/env bash
# mic-calibration.sh — re-apply a calibrated capture gain on the host that
# owns the microphone.
#
# Set MIC_HOST to that machine (user@host, reachable over SSH) and MIC_SOURCE
# to its PipeWire/PulseAudio source name. The values below are the ones
# measured on the author's rig and are included as a worked example.
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
HOST=${MIC_HOST:?set MIC_HOST to the user@host that owns the microphone}
SOURCE=${MIC_SOURCE:-alsa_input.pci-0000_c6_00.6.analog-stereo}

ssh -o BatchMode=yes "$HOST" "pactl set-source-volume $SOURCE $TARGET && \
    pactl list sources | grep -A12 'Name: $SOURCE' | grep -E '^\s+Volume'"
