#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Remote Bridge daemon: streams the TV remote to the Kodi add-on (service.webostv).

Runs as root under its own systemd unit (see 97-kodiremote). Python 2.7 and 3:
webOS 5 ships Python 2, newer firmware Python 3.

Normally it only reads the remote's input devices, so the TV keeps handling
every button. While Kodi is on screen and Kodi asks for it ("grab"), it takes
the remote over (EVIOCGRAB): the TV then sees nothing from it, so it draws no
pointer and shows no input banners, and the daemon hands back to the TV every
button Kodi doesn't use (volume, Home, power...) by writing it into another
remote device. Kodi sends a heartbeat; without one the grab is released.

Protocol, one JSON object per line:
  -> {"hello": {"token", "version", "python", "devices", "pointer": [max_x, max_y]}}
  <- {"ok": true}
  -> {"key": code, "value": 0 up | 1 down | 2 repeat, "dev": index}
  -> {"wheel": steps, "dev": index}
  -> {"pos": [x, y]}                 pointer position, only while grabbed
  <- {"grab": bool, "consume": [key codes Kodi handles], "pointer": bool}
  -> {"cursor": {"point", "hover", "hold", "drag": base64 PNG}}   once per connection:
                                     this TV's own pointer images, so Kodi's cursor
                                     matches it without shipping LG's artwork
"""

from __future__ import print_function

import base64
import errno
import fcntl
import json
import os
import re
import select
import socket
import struct
import sys
import time

VERSION = "0.2.1"
CONFIG = os.environ.get("KODIREMOTE_CONFIG", "/var/lib/webosbrew/kodiremote.json")
LOG = os.environ.get("KODIREMOTE_LOG", "/home/root/kodiremote.log")
DEVICES = os.environ.get("KODIREMOTE_DEVICES", "/proc/bus/input/devices")
INPUT_DIR = os.environ.get("KODIREMOTE_INPUT_DIR", "/dev/input")
DEFAULT_DEVICES = r"^LGE (M-RCU|RCU|TONE|W-RCU|Simple)"
RETRY = 5.0
RESCAN = 30.0
# Release the remote when Kodi has been silent this long.
HEARTBEAT_TIMEOUT = 6.0
# Pointer updates sent to Kodi at most this often.
POS_INTERVAL = 0.016
# The same press can arrive on more than one remote device; drop the copies.
DUP_WINDOW = 0.025

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0, 1, 2, 3
SYN_REPORT = 0
REL_WHEEL = 8
ABS_X, ABS_Y = 0, 1
# struct input_event as this process sees it (the kernel converts for 32-bit).
EVENT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT)
# _IOW('E', 0x90, int) and _IOR('E', 0x40 + axis, struct input_absinfo)
EVIOCGRAB = 0x40044590
EVIOCGABS = 0x80184540
# webOS pointer images: cursorType<KIND>sz<S|M|L>stN.png
CURSOR_DIR = "/usr/share/im"
CURSOR_KINDS = {"point": "POINT", "hover": "HOVER", "hold": "HOLD", "drag": "DRAG"}
CURSOR_MAX = 256 * 1024


def log(text):
    line = "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), text)
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 100000:
            with open(LOG, "rb") as f:
                f.seek(-50000, 2)
                tail = f.read()
            with open(LOG, "wb") as f:
                f.write(tail)
        with open(LOG, "a") as f:
            f.write(line)
    except (IOError, OSError):
        pass


def read_config():
    try:
        with open(CONFIG) as f:
            c = json.load(f)
        if c.get("host") and c.get("port") and c.get("token"):
            return c
    except (IOError, OSError, ValueError):
        pass
    return None


def webos_major():
    try:
        with open("/etc/starfish-release") as f:
            return int(f.read().split()[2].split(".")[0])
    except (IOError, OSError, IndexError, ValueError):
        return 0


def list_devices(pattern):
    try:
        with open(DEVICES) as f:
            text = f.read()
    except (IOError, OSError):
        return []
    found = []
    for block in re.split(r"\n\s*\n", text):
        name = re.search(r'^N: Name="(.*)"$', block, re.M)
        handlers = re.search(r"^H: Handlers=(.*)$", block, re.M)
        event = re.search(r"\bevent\d+\b", handlers.group(1)) if handlers else None
        if name and event and re.search(pattern, name.group(1)):
            found.append((name.group(1), os.path.join(INPUT_DIR, event.group(0))))
    return found


def cursor_images(size="M"):
    images = {}
    for key, kind in CURSOR_KINDS.items():
        path = os.path.join(CURSOR_DIR, "cursorType%ssz%sstN.png" % (kind, size))
        try:
            if os.path.getsize(path) <= CURSOR_MAX:
                with open(path, "rb") as f:
                    images[key] = base64.b64encode(f.read()).decode("ascii")
        except (IOError, OSError):
            pass
    return images


def output_device_name():
    # Magic Mapper's finding: on webOS 10 keys resent through Builtin [2] make
    # Back exit apps instead of closing menus, so use Builtin [1] there.
    return "LGE M-RCU - Builtin [1]" if webos_major() >= 10 else "LGE M-RCU - Builtin [2]"


def abs_max(fd, axis, default):
    try:
        buf = fcntl.ioctl(fd, EVIOCGABS + axis, b"\0" * 24)
        value = struct.unpack("6i", buf)[2]
        return value if value > 0 else default
    except (IOError, OSError, struct.error):
        return default


class Device(object):
    def __init__(self, name, path, index, fd):
        self.name, self.path, self.index, self.fd = name, path, index, fd
        self.grabbed = False
        self.pending = b""
        self.resent = False


class Bridge(object):
    def __init__(self):
        self.config = read_config()
        self.devices = {}          # fd -> Device
        self.count = 0
        self.out_name = output_device_name()
        self.out_fd = None
        self.sock = None
        self.accepted = False
        self.inbuf = b""
        self.last_heard = 0.0
        self.want_grab = False
        self.consume = set()
        self.pointer = False
        self.pos = [0, 0]
        self.pos_dirty = False
        self.pos_sent_at = 0.0
        self.pointer_max = [1920, 1080]
        self.last_event = ("", 0.0, -1)
        self.next_connect = 0.0
        self.next_scan = 0.0
        self.self_stamp = self.stamp()

    # -- devices --

    def scan(self):
        pattern = (self.config or {}).get("devices") or DEFAULT_DEVICES
        open_paths = set(d.path for d in self.devices.values())
        for name, path in list_devices(pattern):
            if path in open_paths or name == self.out_name:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except (IOError, OSError) as e:
                log("cannot open %s (%s): %s" % (path, name, e))
                continue
            dev = Device(name, path, self.count, fd)
            self.count += 1
            self.devices[fd] = dev
            if dev.name.endswith("Builtin [0]"):
                self.pointer_max = [abs_max(fd, ABS_X, 1920), abs_max(fd, ABS_Y, 1080)]
            log("reading %s (%s)" % (path, name))
            if self.want_grab:
                self.set_grab(dev, True)

    def close_device(self, dev):
        self.devices.pop(dev.fd, None)
        try:
            os.close(dev.fd)  # also releases a grab
        except OSError:
            pass
        log("stopped reading %s (%s)" % (dev.path, dev.name))

    def set_grab(self, dev, grab):
        if dev.grabbed == grab:
            return
        try:
            fcntl.ioctl(dev.fd, EVIOCGRAB, 1 if grab else 0)
            dev.grabbed = grab
        except (IOError, OSError) as e:
            log("cannot %s %s: %s" % ("grab" if grab else "release", dev.name, e))

    def apply_grab(self, grab):
        if grab and self.output() is None:
            log("no output device %r: not taking over the remote" % self.out_name)
            grab = False
        if grab != self.want_grab:
            log("remote %s" % ("taken over for Kodi" if grab else "released to the TV"))
        self.want_grab = grab
        for dev in list(self.devices.values()):
            self.set_grab(dev, grab)

    def output(self):
        if self.out_fd is None:
            for name, path in list_devices("^" + re.escape(self.out_name) + "$"):
                try:
                    self.out_fd = os.open(path, os.O_WRONLY)
                except (IOError, OSError) as e:
                    log("cannot open output device %s: %s" % (path, e))
                break
        return self.out_fd

    def resend(self, raw):
        fd = self.output()
        if fd is None:
            return
        try:
            os.write(fd, raw)
        except (IOError, OSError) as e:
            log("resend failed: %s" % e)
            try:
                os.close(fd)
            except OSError:
                pass
            self.out_fd = None

    def read_device(self, dev):
        try:
            data = os.read(dev.fd, EVENT_SIZE * 64)
        except (IOError, OSError) as e:
            if e.errno in (errno.EAGAIN, errno.EINTR):
                return
            data = b""
        if not data:
            self.close_device(dev)
            return
        buf = dev.pending + data
        n = len(buf) - len(buf) % EVENT_SIZE
        for off in range(0, n, EVENT_SIZE):
            raw = buf[off:off + EVENT_SIZE]
            _s, _u, etype, code, value = struct.unpack(EVENT, raw)
            self.on_event(dev, raw, etype, code, value)
        dev.pending = buf[n:]

    def on_event(self, dev, raw, etype, code, value):
        grabbed = dev.grabbed
        if etype == EV_KEY:
            if grabbed and code not in self.consume:
                self.resend(raw)
                dev.resent = True
                return
            self.send_key(dev, code, value)
        elif etype == EV_REL and code == REL_WHEEL and value:
            self.send_unique(dev, "w%d" % value, {"wheel": value, "dev": dev.index})
        elif etype == EV_ABS and code in (ABS_X, ABS_Y):
            if grabbed and self.pointer:
                self.pos[code] = value
                self.pos_dirty = True
            elif grabbed:
                self.resend(raw)
                dev.resent = True
        elif etype == EV_SYN and code == SYN_REPORT:
            if dev.resent:
                self.resend(raw)
                dev.resent = False
            self.flush_pos()
        elif grabbed:
            self.resend(raw)
            dev.resent = True

    def send_key(self, dev, code, value):
        self.send_unique(dev, "k%d:%d" % (code, value), {"key": code, "value": value, "dev": dev.index})

    def send_unique(self, dev, ident, msg):
        now = time.time()
        last_id, last_time, last_dev = self.last_event
        if ident == last_id and dev.index != last_dev and now - last_time < DUP_WINDOW:
            return
        self.last_event = (ident, now, dev.index)
        self.send(msg)

    def flush_pos(self, force=False):
        if not self.pos_dirty:
            return
        now = time.time()
        if force or now - self.pos_sent_at >= POS_INTERVAL:
            self.pos_dirty = False
            self.pos_sent_at = now
            self.send({"pos": self.pos})

    # -- Kodi --

    def send(self, msg):
        if not (self.sock and self.accepted):
            return
        try:
            self.sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        except (IOError, OSError, socket.error):
            self.disconnect()

    def connect(self):
        self.config = read_config()
        if not self.config:
            return
        try:
            sock = socket.create_connection((self.config["host"], int(self.config["port"])), 3)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            hello = {"token": self.config["token"], "version": VERSION,
                     "python": sys.version.split()[0], "pointer": self.pointer_max,
                     "devices": [d.name for d in sorted(self.devices.values(), key=lambda d: d.index)]}
            sock.sendall((json.dumps({"hello": hello}) + "\n").encode("utf-8"))
            sock.setblocking(False)
        except (IOError, OSError, socket.error):
            return
        self.sock, self.accepted, self.inbuf = sock, False, b""
        self.last_heard = time.time()

    def disconnect(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except (IOError, OSError, socket.error):
                pass
            if self.accepted:
                log("Kodi connection closed")
        self.sock, self.accepted = None, False
        self.apply_grab(False)
        self.next_connect = time.time() + RETRY

    def read_socket(self):
        try:
            data = self.sock.recv(4096)
        except (IOError, OSError, socket.error) as e:
            if getattr(e, "errno", None) in (errno.EAGAIN, errno.EINTR):
                return
            data = b""
        if not data:
            self.disconnect()
            return
        self.last_heard = time.time()
        self.inbuf += data
        while b"\n" in self.inbuf:
            line, self.inbuf = self.inbuf.split(b"\n", 1)
            try:
                msg = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            self.on_message(msg)

    def on_message(self, msg):
        if msg.get("ok") and not self.accepted:
            self.accepted = True
            log("connected to Kodi at %s:%s" % (self.config["host"], self.config["port"]))
            images = cursor_images()
            if images:
                self.send({"cursor": images})
        if "grab" in msg:
            self.consume = set(int(c) for c in msg.get("consume") or [])
            self.pointer = bool(msg.get("pointer"))
            self.apply_grab(bool(msg["grab"]) and self.accepted)

    # -- main loop --

    def stamp(self):
        try:
            st = os.stat(os.path.abspath(__file__))
            return "%d:%d" % (st.st_size, int(st.st_mtime))
        except OSError:
            return ""

    def run(self):
        log("Remote Bridge %s started (python %s)" % (VERSION, sys.version.split()[0]))
        while True:
            now = time.time()
            if now >= self.next_scan:
                self.next_scan = now + RESCAN
                current = self.stamp()
                if current and current != self.self_stamp:
                    log("app updated, restarting")
                    return
                self.scan()
            if self.sock is None and now >= self.next_connect:
                self.connect()
                if self.sock is None:
                    self.next_connect = now + RETRY
            if self.want_grab and now - self.last_heard > HEARTBEAT_TIMEOUT:
                log("no heartbeat from Kodi")
                self.apply_grab(False)
            fds = list(self.devices)
            if self.sock is not None:
                fds.append(self.sock.fileno())
            timeout = POS_INTERVAL if self.pos_dirty else 1.0
            try:
                readable = select.select(fds, [], [], timeout)[0]
            except (IOError, OSError, select.error) as e:
                if e.args and e.args[0] == errno.EINTR:
                    continue
                raise
            for fd in readable:
                if self.sock is not None and fd == self.sock.fileno():
                    self.read_socket()
                elif fd in self.devices:
                    self.read_device(self.devices[fd])
            self.flush_pos()


if __name__ == "__main__":
    Bridge().run()
