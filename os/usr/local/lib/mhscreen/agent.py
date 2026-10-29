#!/usr/bin/env python3
"""MakeHaven screen agent.

Runs on every screen Pi as root. It decides what the TV shows and takes
commands, so the browser itself never has to be configured by hand.

- Identity is the Pi's hardware serial number. The last six hex digits are the
  Screen ID people read off the TV and type into the website.
- What a screen shows comes from the website's screen registry
  (GET <config_base>/display/screen/<ID>/config). The last good answer is
  cached on the boot partition, so a screen comes back after a power cut even
  when the website or the internet is down.
- Commands are pushed, not polled: the agent holds one long-lived connection to
  an ntfy topic. Every command is HMAC-signed and time-limited, so knowing the
  topic name is not enough to drive a screen.
- The browser always starts on the local launcher page (served here on
  127.0.0.1:8080). The launcher shows the Screen ID, a clock and a plain-words
  status, and moves on to the assigned page once it is reachable.

Standard library only: the image must never depend on pip at boot.
"""

import hashlib
import hmac
import http.server
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.1.0"
# Paths can be overridden for testing off a Pi.
CONF_PATH = os.environ.get("MHSCREEN_CONF", "/etc/mhscreen/screen.json")
CACHE_PATH = os.environ.get("MHSCREEN_CACHE", "/boot/firmware/mhscreen-assignment.json")
LAUNCHER_PATH = os.environ.get("MHSCREEN_LAUNCHER", "/usr/local/lib/mhscreen/launcher.html")
ROTATION_PATH = os.environ.get("MHSCREEN_ROTATION", "/run/mhscreen/rotation")
CEC_SCRIPT = os.environ.get("MHSCREEN_CEC", "/usr/local/lib/mhscreen/cec-reclaim.sh")
LISTEN = ("127.0.0.1", int(os.environ.get("MHSCREEN_PORT", "8080")))
RESTART_BROWSER = os.environ.get("MHSCREEN_RESTART", "systemctl restart --no-block mhscreen-kiosk.service").split()

# Commands older than this are ignored, so a replayed message does nothing.
MAX_COMMAND_AGE = 300
# Re-ask the registry this often even without a push, as a safety net.
CONFIG_REFRESH = 6 * 3600
# While a freshly started screen is unassigned, someone is probably standing at
# the TV waiting for it to be assigned: ask every minute for the first half hour.
PAIRING_REFRESH = 60
PAIRING_WINDOW = 30 * 60
# An outage at least this long gets a browser restart once the network is back,
# because Chromium may be sitting on its own error page with no script running.
OUTAGE_RESTART_AFTER = 120


def log(msg):
    print(msg, flush=True)


def run(argv, timeout=30):
    """Run a helper without ever letting it block the command channel."""
    try:
        subprocess.run(argv, check=False, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"{argv[0]} failed: {exc}")


def read_serial():
    for path in ("/proc/device-tree/serial-number", "/sys/firmware/devicetree/base/serial-number"):
        try:
            with open(path, "rb") as fh:
                serial = fh.read().strip(b"\0\n ").decode()
                if serial:
                    return serial.lower()
        except OSError:
            pass
    # Not a Pi (e.g. testing on a laptop): fall back to the machine id.
    with open("/etc/machine-id") as fh:
        return fh.read().strip()[:16]


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET; nothing is sent.
            return s.getsockname()[0]
    except OSError:
        return ""


def root_is_overlay():
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "/":
                    return parts[2] == "overlay"
    except OSError:
        pass
    return False


class Agent:

    def __init__(self, conf):
        self.conf = conf
        self.serial = read_serial()
        self.screen_id = self.serial[-6:].upper()
        self.lock = threading.Lock()
        self.assignment = self._load_cache()
        self.registry_ok = False
        self.online = False
        self.ever_online = False
        self.offline_since = time.monotonic()
        self.target_checked = (0.0, None, False)
        self.wake = threading.Event()

    # --- assignment -------------------------------------------------------

    def _load_cache(self):
        try:
            with open(CACHE_PATH) as fh:
                data = json.load(fh)
                if isinstance(data, dict) and data.get("url"):
                    return data
        except (OSError, ValueError):
            pass
        return None

    def _save_cache(self, data):
        # The boot partition sits outside the read-only overlay. Only write when
        # the assignment actually changed, to keep card wear near zero.
        if data == self._load_cache():
            return
        try:
            if data:
                tmp = CACHE_PATH + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump(data, fh)
                os.replace(tmp, CACHE_PATH)
            elif os.path.exists(CACHE_PATH):
                os.remove(CACHE_PATH)
        except OSError as exc:
            log(f"could not write assignment cache: {exc}")

    def _apply(self, data, source):
        """Adopt a new assignment; restart the browser if it changed."""
        with self.lock:
            changed = (data or {}).get("url") != (self.assignment or {}).get("url") \
                or (data or {}).get("rotation", 0) != (self.assignment or {}).get("rotation", 0)
            self.assignment = data
        self._save_cache(data)
        self._write_rotation()
        if changed:
            log(f"assignment from {source}: {data}")
            self.restart_browser()

    def _write_rotation(self):
        rotation = str((self.assignment or {}).get("rotation", 0))
        if rotation not in ("0", "90", "180", "270"):
            rotation = "0"
        try:
            os.makedirs(os.path.dirname(ROTATION_PATH), exist_ok=True)
            with open(ROTATION_PATH, "w") as fh:
                fh.write(rotation)
        except OSError:
            pass

    def fetch_config(self):
        """Ask the website what this screen shows. Returns True when answered."""
        base = self.conf["config_base"].rstrip("/")
        url = f"{base}/display/screen/{self.screen_id}/config?agent={VERSION}"
        req = urllib.request.Request(url, headers={"User-Agent": f"mhscreen/{VERSION} ({self.screen_id})"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            # 404 = the registry is not deployed yet; keep any pushed assignment.
            log(f"registry answered HTTP {exc.code}")
            self.mark_online()
            return exc.code == 404
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"registry unreachable: {exc}")
            return False
        self.mark_online()
        self.registry_ok = True
        if data.get("assigned") and data.get("url"):
            target = urllib.parse.urljoin(base + "/", data["url"])
            self._apply({"url": target, "label": data.get("label", ""),
                         "rotation": int(data.get("rotation", 0) or 0)}, "registry")
        else:
            self._apply(None, "registry")
        return True

    def config_loop(self):
        started = time.monotonic()
        delay = 10
        next_refresh = 0.0
        while True:
            if time.monotonic() >= next_refresh or self.wake.is_set():
                self.wake.clear()
                if self.fetch_config():
                    delay = 10
                    pairing = self.assignment is None and time.monotonic() - started < PAIRING_WINDOW
                    next_refresh = time.monotonic() + (PAIRING_REFRESH if pairing else CONFIG_REFRESH)
                else:
                    next_refresh = time.monotonic() + delay
                    delay = min(delay * 2, 300)
            self.wake.wait(timeout=max(1.0, next_refresh - time.monotonic()))

    # --- network state ----------------------------------------------------

    def mark_online(self):
        restart = False
        with self.lock:
            if not self.online:
                outage = time.monotonic() - self.offline_since
                restart = self.ever_online and self.assignment is not None \
                    and outage >= OUTAGE_RESTART_AFTER
                self.online = True
                self.ever_online = True
        if restart:
            log("network back after an outage; restarting the browser")
            self.restart_browser()

    def mark_offline(self):
        with self.lock:
            if self.online:
                self.online = False
                self.offline_since = time.monotonic()

    # --- commands ---------------------------------------------------------

    def verify(self, msg):
        try:
            to, cmd, ts, sig = msg["to"], msg["cmd"], int(msg["ts"]), msg["sig"]
        except (KeyError, TypeError, ValueError):
            return False
        if to not in ("all", self.screen_id):
            return False
        if abs(time.time() - ts) > MAX_COMMAND_AGE:
            log(f"ignoring stale command {cmd}")
            return False
        args = msg.get("args") or {}
        payload = f"{to}|{cmd}|{json.dumps(args, sort_keys=True, separators=(',', ':'))}|{ts}"
        expected = hmac.new(self.conf["cmd_secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, str(sig)):
            log(f"bad signature on command {cmd}")
            return False
        return True

    def handle(self, msg):
        cmd = msg["cmd"]
        args = msg.get("args") or {}
        log(f"command: {cmd} {args}")
        if cmd == "reload":
            self.wake.set()
            self.restart_browser()
        elif cmd == "reboot":
            run(["systemctl", "reboot"])
        elif cmd == "cec":
            run([CEC_SCRIPT], timeout=60)
        elif cmd == "assign" and args.get("url"):
            self._apply({"url": args["url"], "label": args.get("label", ""),
                         "rotation": int(args.get("rotation", 0) or 0)}, "command")
        elif cmd == "unassign":
            self._apply(None, "command")
        elif cmd == "ping":
            self.publish_status()
        else:
            log(f"unknown command {cmd}")

    def publish_status(self):
        status = self.status()
        url = f"{self.conf['ntfy_url'].rstrip('/')}/{self.conf['ntfy_topic']}-status"
        body = json.dumps(status).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST"), timeout=15).close()
        except (urllib.error.URLError, OSError) as exc:
            log(f"could not publish status: {exc}")

    def command_loop(self):
        """Hold the ntfy subscription open; reconnect with backoff."""
        url = f"{self.conf['ntfy_url'].rstrip('/')}/{self.conf['ntfy_topic']}/json"
        delay = 5
        while True:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": f"mhscreen/{VERSION} ({self.screen_id})"})
                # ntfy sends a keepalive every 45s, so a 90s silence means a dead link.
                with urllib.request.urlopen(req, timeout=90) as resp:
                    self.mark_online()
                    delay = 5
                    for raw in resp:
                        event = json.loads(raw)
                        if event.get("event") != "message":
                            continue
                        try:
                            msg = json.loads(event.get("message", ""))
                        except ValueError:
                            continue
                        if self.verify(msg):
                            self.handle(msg)
            except Exception as exc:  # noqa: BLE001 - this loop must never die.
                log(f"command channel down: {exc}")
            self.mark_offline()
            time.sleep(delay)
            delay = min(delay * 2, 120)

    # --- browser ----------------------------------------------------------

    def restart_browser(self):
        run(RESTART_BROWSER)

    def target_reachable(self, url):
        """True when the assigned page answers below 500 (checked at most every 30s).

        Sending the browser to a page mid-deploy would strand it on an error
        page, so the launcher only moves on once the page itself answers.
        """
        checked_at, checked_url, ok = self.target_checked
        if checked_url == url and time.monotonic() - checked_at < 30:
            return ok
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": f"mhscreen/{VERSION} ({self.screen_id})"})
        try:
            with urllib.request.urlopen(req, timeout=15):
                ok = True
        except urllib.error.HTTPError as exc:
            ok = exc.code < 500
        except (urllib.error.URLError, OSError):
            ok = False
        self.target_checked = (time.monotonic(), url, ok)
        return ok

    def status(self):
        with self.lock:
            a = self.assignment
            online = self.online
        if a and online and not self.target_reachable(a["url"]):
            state, message = "waiting", "The website is not answering. Retrying, the display will come back on its own."
        elif a and online:
            state, message = "assigned", f"Opening {a.get('label') or 'display'}"
        elif a:
            state, message = "offline", "No network. Retrying, the display will come back on its own."
        elif online:
            state, message = "unassigned", "Waiting to be assigned. Send this Screen ID to JR."
        else:
            state, message = "offline", "No network. Check the network cable or wifi."
        return {
            "id": self.screen_id,
            "state": state,
            "message": message,
            "url": a["url"] if state == "assigned" else None,
            "label": (a or {}).get("label", ""),
            "hostname": socket.gethostname(),
            "ip": local_ip(),
            "overlay": root_is_overlay(),
            "version": VERSION,
        }


def make_handler(agent):

    class Handler(http.server.BaseHTTPRequestHandler):

        def do_GET(self):
            if self.path.startswith("/status"):
                body = json.dumps(agent.status()).encode()
                ctype = "application/json"
            else:
                with open(LAUNCHER_PATH, "rb") as fh:
                    body = fh.read()
                ctype = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def main():
    with open(CONF_PATH) as fh:
        conf = json.load(fh)
    agent = Agent(conf)
    log(f"mhscreen agent {VERSION}, Screen ID {agent.screen_id}")
    agent._write_rotation()
    threading.Thread(target=agent.config_loop, daemon=True).start()
    threading.Thread(target=agent.command_loop, daemon=True).start()
    server = http.server.ThreadingHTTPServer(LISTEN, make_handler(agent))
    server.serve_forever()


if __name__ == "__main__":
    main()
