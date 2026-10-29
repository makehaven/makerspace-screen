#!/bin/bash
# Build MakeHaven-Screen.img.xz: the official Raspberry Pi OS Lite image with
# the screen software baked in. Every screen is flashed from this one file.
#
#   build/build-image.sh ~/.cache/mhscreen/2026-10-06-raspios-trixie-arm64-lite.img.xz
#
# Needs Docker (no sudo). Secrets (wifi password, command key) come from
# ~/.config/makehaven/screen-build.env and are never committed; see README.
# The output therefore contains secrets: share it only with the person flashing.
set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
secrets=${MHSCREEN_SECRETS:-$HOME/.config/makehaven/screen-build.env}
cache=${MHSCREEN_CACHE:-$HOME/.cache/mhscreen}
base=${1:?usage: build-image.sh <raspios-lite-arm64.img.xz>}
out="$cache/MakeHaven-Screen.img"

[ -f "$secrets" ] || { echo "missing $secrets (see README)" >&2; exit 1; }
mkdir -p "$cache"

echo "== registering arm64 emulation"
docker run --rm --privileged tonistiigi/binfmt --install arm64 >/dev/null

echo "== unpacking base image"
xz -dc "$base" >"$out"
# Room for Chromium and friends; the Pi grows the root to the full card on first boot.
truncate -s +2G "$out"

echo "== customising (several minutes: arm64 packages install under emulation)"
docker run --rm --privileged \
  -v /dev:/dev \
  -v "$here:/src:ro" \
  -v "$cache:/work" \
  -v "$secrets:/secrets.env:ro" \
  debian:trixie bash /src/build/inside.sh "/work/$(basename "$out")"

echo "== compressing"
xz -T0 -6 -f "$out"
ls -lh "$out.xz"
sha256sum "$out.xz" | tee "$out.xz.sha256"
