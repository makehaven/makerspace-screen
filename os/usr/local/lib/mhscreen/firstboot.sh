#!/bin/bash
# One-time setup on a screen's very first boot, then reboot into read-only mode.
# Every card is written from the same image; this is what makes each Pi itself.
set -u

serial=$(tr -d '\0' </proc/device-tree/serial-number)
id=${serial: -6}
host="mhscreen-${id,,}"

echo "$host" >/etc/hostname
sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$host/" /etc/hosts
hostname "$host"

# Every card starts with the same image, so each Pi needs its own SSH host keys.
rm -f /etc/ssh/ssh_host_*
ssh-keygen -A

raspi-config nonint do_wifi_country US || true

touch /etc/mhscreen/provisioned

# Read-only root: all writes go to RAM and vanish at reboot, so power cuts and
# write wear can no longer kill the card. Read-only mode is undone with
#   sudo raspi-config nonint do_overlayfs 1   (then reboot)
if [ -f /etc/mhscreen/read-only ]; then
  if raspi-config nonint do_overlayfs 0; then
    # Make sure the initramfs carries the overlayroot hook for this kernel.
    update-initramfs -u -k all || echo "initramfs rebuild failed" >&2
  else
    echo "overlay setup failed; staying writable" >&2
  fi
fi

sync
systemctl reboot
