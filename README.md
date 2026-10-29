# MakeHaven Screens

One SD-card image for every TV and panel in the building. Flash it, plug it in,
and the screen shows its **Screen ID**. Assign that ID on the website and the
screen shows its page from then on.

Nothing is configured on the Pi itself. What a screen shows lives in the
website's screen registry (`/admin/config/system/screens`, module
`makerspace_kiosk`). Commands (reload, reboot, switch the TV input back) are
**pushed** to the screen, not polled.

Staff setup guide (shopping list + steps, for whoever installs a screen):
<https://docs.google.com/document/d/1XtgP8n7aY-EaheI_4bBzdDJLLm4cDQKHYI5XMKOI2BY/edit> (Google Doc in the "Digital Display Screens" Drive folder, next to the image)

## How a screen works

```
power on
  └─ firstboot (first boot only): hostname mhscreen-<id>, fresh SSH host keys,
     switch root to read-only (overlayfs), reboot
  └─ mhscreen-agent (root, Python stdlib)
       ├─ asks  <site>/display/screen/<ID>/config      at boot, on "reload", every 6h
       │        (every minute for the first 30 min while unassigned)
       ├─ holds one long-lived connection to the ntfy topic for signed commands
       └─ serves the launcher page on 127.0.0.1:8080
  └─ mhscreen-kiosk: cage (one-window Wayland) + Chromium, always starting on the
     launcher. The launcher shows ID, clock and status, and moves on to the
     assigned page once that page answers (never into a deploy's 5xx).
nightly: 03:00 reboot, 04:00 HDMI-CEC "switch the TV back to me"
```

Recovery, layer by layer:

| Failure | What happens |
|---|---|
| Browser crashes | systemd restarts cage+Chromium in 3s |
| Whole Pi hangs | hardware watchdog resets it (15s) |
| Power cut / yanked plug | read-only root: nothing to corrupt; boots straight back |
| Internet or website down at boot | launcher shows "No network" or "website not answering" and retries; the last assignment is cached on the boot partition |
| Outage while showing a page | when the command channel reconnects after ≥2 min down, the browser restarts onto the launcher |
| Someone changes the TV input | 04:00 CEC reclaim; or send `cec` now |
| Anything else | 03:00 reboot |

The *page* itself (slideshow etc.) handles in-page staleness and deploys through
the `makerspace_kiosk` resilience runtime.

## Identity

The Screen ID is the last six hex digits of the Pi's hardware serial number
(`/proc/device-tree/serial-number`). Same card in a different Pi = a different
screen; that is intended (the *Pi* is what is mounted behind a TV).

## Building the image

```bash
build/build-image.sh ~/.cache/mhscreen/2026-10-06-raspios-trixie-arm64-lite.img.xz
# -> ~/.cache/mhscreen/MakeHaven-Screen.img.xz (+ .sha256)
```

Needs Docker only (no sudo): it registers arm64 emulation, then customises the
official Raspberry Pi OS Lite image in a privileged container. Takes a while:
Chromium installs under emulation.

Settings come from `~/.config/makehaven/screen-build.env` (mode 600, never
committed):

```bash
CONFIG_BASE=https://www.makehaven.org
NTFY_URL=https://ntfy.sh
NTFY_TOPIC=mhscreen-<random>
CMD_SECRET=<64 hex chars>          # signs commands
WIFI_SSID=mhvinfra
WIFI_PSK=...
ADMIN_SSH_KEY="ssh-ed25519 ..."    # SSH as makehaven@mhscreen-<id>.local, key only
READ_ONLY=1
```

**The built image contains the wifi password and the command key.** Share it
with the person flashing it, never by public link.

Base-image notes (Trixie, 2026-10-06): cloud-init is disabled (it is only how
Raspberry Pi Imager customises a card, and would rename the Pi); the
`userconfig` first-boot wizard is disabled; `/etc/machine-id` is left
`uninitialized` so the stock first-boot filesystem grow still runs.

## Commands

```bash
tools/mhscreen-cmd <ID|all> ping       # prints the screen's status
tools/mhscreen-cmd <ID> cec            # switch the TV back to the Pi now
tools/mhscreen-cmd <ID> reload         # re-ask the registry, restart the browser
tools/mhscreen-cmd <ID> reboot
tools/mhscreen-cmd <ID> assign url=/display/slideshow label="Main TV"   # stop-gap, no website needed
tools/mhscreen-cmd <ID> unassign
```

Commands are HMAC-SHA256 signed with `CMD_SECRET` and rejected if more than
5 minutes old, so the topic name alone cannot drive a screen. A registry answer
overrides a pushed `assign` at the next ask.

## Fixing a screen by hand

```bash
ssh makehaven@mhscreen-<id>.local
journalctl -u mhscreen-agent -u mhscreen-kiosk -b
sudo raspi-config nonint do_overlayfs 1 && sudo reboot   # make writable to change things
# ...change things...
sudo raspi-config nonint do_overlayfs 0 && sudo reboot   # back to read-only
```

## Testing the agent off a Pi

Every path is overridable, so the agent runs on any Linux box:

```bash
MHSCREEN_CONF=/tmp/t/screen.json MHSCREEN_CACHE=/tmp/t/cache.json \
MHSCREEN_ROTATION=/tmp/t/rot MHSCREEN_CEC=/bin/true MHSCREEN_RESTART=/bin/true \
MHSCREEN_PORT=18080 MHSCREEN_LAUNCHER=$PWD/os/usr/local/lib/mhscreen/launcher.html \
python3 os/usr/local/lib/mhscreen/agent.py
```

## Next

- **LAN relay** on the Proxmox host: self-hosted ntfy (same protocol, change
  `NTFY_URL`) plus one cached copy of each display feed, so N screens cost one
  request to Pantheon and survive deploys and internet outages.
- **Website pushes reload** when an assignment changes (needs the command key
  in Pantheon Secrets).
- **ESP32 panels** (ESPHome/LVGL) and **tablets** (Fully Kiosk) join the same
  registry by MAC / device ID.
- **Netboot** for wired Pis: no SD card at all.
