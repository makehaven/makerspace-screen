#!/bin/bash
# Runs inside cage (the one-window Wayland compositor) as the "screen" user.
# Applies the screen's rotation, then hands the whole display to Chromium.

rotation=$(cat /run/mhscreen/rotation 2>/dev/null || echo 0)
if [ "$rotation" != "0" ]; then
  output=$(wlr-randr | awk 'NR==1 {print $1}')
  [ -n "$output" ] && wlr-randr --output "$output" --transform "$rotation"
fi

# A fresh profile every start: nothing to corrupt, no "restore pages?" bubble.
profile=/tmp/mhscreen-chromium
rm -rf "$profile"

exec chromium \
  --kiosk \
  --ozone-platform=wayland \
  --user-data-dir="$profile" \
  --noerrdialogs \
  --disable-infobars \
  --no-first-run \
  --disable-session-crashed-bubble \
  --disable-features=Translate,MediaRouter \
  --password-store=basic \
  --autoplay-policy=no-user-gesture-required \
  --check-for-update-interval=31536000 \
  http://127.0.0.1:8080/
