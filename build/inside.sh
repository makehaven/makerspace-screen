#!/bin/bash
# Runs inside a privileged Debian container: grows the image, mounts it,
# installs the kiosk stack in an arm64 chroot and copies in /src/os.
set -euo pipefail
img=$1
# shellcheck disable=SC1091
. /secrets.env

apt-get update -qq
apt-get install -y -qq parted e2fsprogs fdisk >/dev/null

parted -s "$img" resizepart 2 100%
part() { sfdisk -d "$img" | awk -F'[=,]' -v p="${img}$1 :" 'index($0, p) == 1 {print $2+0, $4+0}'; }
read -r boot_start boot_size < <(part 1)
read -r root_start root_size < <(part 2)

root_loop=$(losetup -f --show -o $((root_start * 512)) --sizelimit $((root_size * 512)) "$img")
boot_loop=$(losetup -f --show -o $((boot_start * 512)) --sizelimit $((boot_size * 512)) "$img")
cleanup() {
  for m in dev/pts dev proc sys boot/firmware ""; do umount "/mnt/root/$m" 2>/dev/null || true; done
  losetup -d "$root_loop" "$boot_loop" 2>/dev/null || true
}
trap cleanup EXIT

e2fsck -fy "$root_loop" >/dev/null || true
resize2fs "$root_loop" >/dev/null
mkdir -p /mnt/root
mount "$root_loop" /mnt/root
mount "$boot_loop" /mnt/root/boot/firmware
mount -t proc proc /mnt/root/proc
mount -t sysfs sys /mnt/root/sys
mount --bind /dev /mnt/root/dev
mount --bind /dev/pts /mnt/root/dev/pts
machine_id_before=$(cat /mnt/root/etc/machine-id)
mv /mnt/root/etc/resolv.conf /mnt/root/etc/resolv.conf.orig 2>/dev/null || true
cp /etc/resolv.conf /mnt/root/etc/resolv.conf

# The software itself, owned by root (the build user's uid 1000 is "pi" in the
# image). --no-overwrite-dir leaves existing directories such as /etc untouched.
tar -C /src/os -cf - --owner=0 --group=0 --exclude=__pycache__ . \
  | tar -C /mnt/root -xf - --no-overwrite-dir

# Per-fleet settings and secrets.
mkdir -p /mnt/root/etc/mhscreen
cat >/mnt/root/etc/mhscreen/screen.json <<JSON
{
  "config_base": "${CONFIG_BASE}",
  "ntfy_url": "${NTFY_URL}",
  "ntfy_topic": "${NTFY_TOPIC}",
  "cmd_secret": "${CMD_SECRET}"
}
JSON
chmod 600 /mnt/root/etc/mhscreen/screen.json
[ "${READ_ONLY:-1}" = "1" ] && touch /mnt/root/etc/mhscreen/read-only

if [ -n "${WIFI_SSID:-}" ]; then
  conn="/mnt/root/etc/NetworkManager/system-connections/${WIFI_SSID}.nmconnection"
  mkdir -p "$(dirname "$conn")"
  cat >"$conn" <<NM
[connection]
id=${WIFI_SSID}
type=wifi
autoconnect=true

[wifi]
mode=infrastructure
ssid=${WIFI_SSID}

[wifi-security]
key-mgmt=wpa-psk
psk=${WIFI_PSK}

[ipv4]
method=auto

[ipv6]
method=auto
NM
  chmod 600 "$conn"
fi

admin_key=${ADMIN_SSH_KEY:-}
chroot /mnt/root /bin/bash -euo pipefail <<CHROOT
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  cage chromium wlr-randr v4l-utils fonts-dejavu-core overlayroot >/dev/null
apt-get clean
rm -rf /var/lib/apt/lists/*

# The browser runs as "screen" (no password, no login). People fix things over
# SSH as "makehaven", key only.
id screen >/dev/null 2>&1 || useradd -m -s /usr/sbin/nologin -G video,render,input,audio screen
id makehaven >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo,video makehaven
passwd -l makehaven >/dev/null
echo 'makehaven ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/010-makehaven
chmod 440 /etc/sudoers.d/010-makehaven
install -d -m 700 -o makehaven -g makehaven /home/makehaven/.ssh
if [ -n "$admin_key" ]; then
  echo "$admin_key" >/home/makehaven/.ssh/authorized_keys
  chown makehaven:makehaven /home/makehaven/.ssh/authorized_keys
  chmod 600 /home/makehaven/.ssh/authorized_keys
fi
mkdir -p /etc/ssh/sshd_config.d
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' >/etc/ssh/sshd_config.d/10-mhscreen.conf

# No first-boot "create a user" wizard: the image already has its users.
systemctl disable userconfig.service 2>/dev/null || true
rm -f /etc/ssh/sshd_config.d/rename_user.conf
# cloud-init is how Raspberry Pi Imager customises a card. These cards are not
# customised (Kate answers "No"), and left on it would rename the Pi and reset
# its users on first boot. Our own first-boot script does that job instead.
touch /etc/cloud/cloud-init.disabled

ln -sf /usr/share/zoneinfo/America/New_York /etc/localtime
echo America/New_York >/etc/timezone

systemctl enable ssh mhscreen-firstboot.service mhscreen-agent.service \
  mhscreen-kiosk.service mhscreen-reboot.timer mhscreen-cec.timer
systemctl set-default graphical.target
CHROOT

# Never blank the console; keep the screen black-and-quiet while booting.
cmdline=/mnt/root/boot/firmware/cmdline.txt
grep -q consoleblank "$cmdline" || sed -i '1 s/$/ consoleblank=0/' "$cmdline"

mv /mnt/root/etc/resolv.conf.orig /mnt/root/etc/resolv.conf 2>/dev/null || rm -f /mnt/root/etc/resolv.conf

# systemd's first-boot detection (which grows the filesystem) keys off this file.
printf '%s\n' "$machine_id_before" >/mnt/root/etc/machine-id

grep -q overlayroot /mnt/root/usr/share/initramfs-tools/hooks/* 2>/dev/null \
  && echo "overlayroot initramfs hook present" \
  || echo "WARNING: overlayroot hook not found; firstboot will rebuild the initramfs"
echo "image customised"
