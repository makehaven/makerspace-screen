#!/bin/bash
# Switch the TV back to this Pi over HDMI-CEC: wake the TV, then announce this
# Pi as the active source so the TV changes input. Runs nightly at 04:00 and on
# the "cec" command. Needs the TV's CEC setting on (Anynet+, SimpLink, Bravia
# Sync, ...) and the cable in the Pi's HDMI0 port.

for dev in /dev/cec*; do
  [ -e "$dev" ] || continue
  cec-ctl -d "$dev" --playback --osd-name MakeHaven >/dev/null 2>&1 || continue
  pa=$(cec-ctl -d "$dev" | awk -F': *' '/Physical Address/ {print $2; exit}' | tr -d ' ')
  if [ -z "$pa" ] || [ "$pa" = "f.f.f.f" ]; then
    continue  # nothing CEC-capable on this port
  fi
  echo "using $dev, physical address $pa"
  cec-ctl -d "$dev" --to 0 --image-view-on
  sleep 3
  cec-ctl -d "$dev" --active-source phys-addr="$pa"
  exit 0
done
echo "no HDMI-CEC connection found: is the cable in HDMI0 and CEC on in the TV menu?" >&2
exit 1
