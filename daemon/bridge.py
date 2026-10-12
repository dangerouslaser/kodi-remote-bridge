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
  -> {"cursor": {"point", "hover", "hold", "drag": base64 PNG}, "hotspot": rule}
                                     this TV's own pointer images (on connecting, and
                                     when the pointer size or shape changes), so Kodi's
                                     cursor matches it without shipping LG's artwork.
                                     rule: "corner" (click point at the shape's
                                     top-left corner), "corner-tr", "corner-bl",
                                     "corner-br", or "tip" (its topmost point)
  -> {"tv": {setting: value}}        the TV's pointer settings (TV_SETTINGS), on
                                     connecting and whenever one changes
  <- {"set_tv": {setting: value}}    change pointer settings on the TV
  <- {"warp": [x, y]}                move the TV's pointer (the remote's absolute
                                     position) there, e.g. onto what Kodi has focused
  -> {"warped": [x, y]}              it's there
  <- {"press": code}                 press and release that button for the TV (a
                                     button Kodi took, e.g. a held Home)
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
import subprocess
import sys
import time

VERSION = "0.5.0"
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
CURSOR_DIR = "/usr/share/im"
# The current webOS pointer (a translucent circle with one pointed corner):
# cursorTypeAsz<S|M|L>st<frame>.png. S0 is its resting frame, P0 pressed.
CURSOR_CURRENT = {"point": "S0", "hover": "S0", "hold": "P0", "drag": "S0"}
# The older hand and teardrop: cursorType<KIND>sz<S|M|L>stN.png.
CURSOR_CLASSIC = {"point": "POINT", "hover": "HOVER", "hold": "HOLD", "drag": "DRAG"}
CURSOR_MAX = 256 * 1024
# The pointer settings (Settings > General > Devices > Pointer settings) and
# their settings-service category.
TV_SETTINGS = {
    "pointerSpeed": "option",      # slow | normal | fast
    "pointerSize": "option",       # small | medium | large
    "pointerShape": "option",      # auto | a | b | c | d
    "pointerAlignment": "option",  # on | off
    "cursorAutoRemover": "other",  # on | off: hide the pointer when it's idle
}
SETTINGS_URI = "luna://com.webos.settingsservice/"
CURSOR_SIZES = {"small": "S", "medium": "M", "large": "L"}
# pointerShape -> the pointer's frame-name prefix and where its click point is.
# The current pointer comes pointing four ways: S0 (top-left), QS0 (top-right),
# TS0 (bottom-left), HS0 (bottom-right); P0, QP0... are the pressed frames.
CURSOR_SHAPES = {
    "a": ("", "corner"), "b": ("Q", "corner-tr"),
    "c": ("T", "corner-bl"), "d": ("H", "corner-br"),
}
# A pointer move the TV refuses (the remote asleep) is retried this long.
WARP_TIME = 0.6
# Handing the remote back, the pointer goes where Kodi's was: within this long.
HANDBACK_TIME = 2.0
# The Magic Remote's pointer signals (on its pointer device, e.g. Builtin [0]):
# it wakes ("show", before the first motion or wheel notch) and goes idle
# ("hide"). The TV shows its pointer on "show" only, so one it misses while
# the remote is taken over leaves its pointer dead for that whole motion,
# until the remote rests again. Not buttons: never sent to Kodi.
CURSOR_SHOW, CURSOR_HIDE = 1198, 1199
LUNA_SEND = "luna-send"
# On connecting, wait this long for the TV's settings before sending the cursor.
SETTINGS_WAIT = 3.0


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


def cursor_images(settings=None):
    """(kind -> base64 PNG, hotspot rule) for this TV's pointer as set up
    (size, shape), or ({}, None)."""
    settings = settings or {}
    size = CURSOR_SIZES.get(settings.get("pointerSize"), "M")
    # "auto" (and anything unknown): the pointer's usual top-left shape.
    prefix, corner = CURSOR_SHAPES.get(settings.get("pointerShape"), CURSOR_SHAPES["a"])
    current = dict((k, "cursorTypeAsz%sst%s%s.png" % (size, prefix, frame))
                   for k, frame in CURSOR_CURRENT.items())
    plain = dict((k, "cursorTypeAsz%sst%s.png" % (size, frame)) for k, frame in CURSOR_CURRENT.items())
    classic = dict((k, "cursorType%ssz%sstN.png" % (kind, size)) for k, kind in CURSOR_CLASSIC.items())
    for names, hotspot in ((current, corner), (plain, "corner"), (classic, "tip")):
        images = {}
        for key, name in names.items():
            path = os.path.join(CURSOR_DIR, name)
            try:
                if os.path.getsize(path) <= CURSOR_MAX:
                    with open(path, "rb") as f:
                        images[key] = base64.b64encode(f.read()).decode("ascii")
            except (IOError, OSError):
                pass
        if "point" in images:
            return images, hotspot
    return {}, None


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


class Luna(object):
    """A luna-send call running in the background; its replies are JSON lines.

    luna-send forks quickly but replies take a while; the main loop keeps
    reading the remote meanwhile, and select()s on the reply pipe. A
    subscription (luna-send -i) lasts while its stdin stays open, so it gets
    a pipe that's only closed with it (the daemon's own stdin is /dev/null).
    """

    def __init__(self, uri, params, on_reply, subscribe=False):
        self.on_reply = on_reply
        self.buf = b""
        args = [LUNA_SEND, "-i" if subscribe else "-n", "1", uri, json.dumps(params)]
        if subscribe:
            del args[2]
        with open(os.devnull, "wb") as null:
            self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=null)
        self.fd = self.proc.stdout.fileno()

    def read(self):
        """Handle what's arrived; False once luna-send has finished."""
        try:
            data = os.read(self.fd, 65536)
        except (IOError, OSError) as e:
            if e.errno in (errno.EAGAIN, errno.EINTR):
                return True
            data = b""
        if data:
            self.buf += data
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                self.reply(line)
            return True
        self.reply(self.buf)
        self.close()
        return False

    def reply(self, line):
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            return
        if isinstance(msg, dict):
            self.on_reply(msg)

    def close(self):
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                pipe.close()
            except (IOError, OSError):
                pass
        if self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc.wait()


class Device(object):
    def __init__(self, name, path, index, fd):
        self.name, self.path, self.index, self.fd = name, path, index, fd
        self.grabbed = False
        self.pending = b""
        self.resent = False
        self.cursor = False  # the remote's last pointer signal was "show"
        self.echoes = set()  # replayed signals still to read back (not the remote's)


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
        self.pos_seen = False
        self.pos_sent_at = 0.0
        self.pointer_max = [1920, 1080]
        self.last_event = ("", 0.0, -1)
        self.next_connect = 0.0
        self.next_scan = 0.0
        self.self_stamp = self.stamp()
        self.luna = {}             # fd -> Luna
        self.watching = {}         # settings category -> its subscription
        self.tv = {}               # TV_SETTINGS -> value
        self.tv_sent = None
        self.cursor_sent = None
        self.accepted_at = 0.0
        self.warp_to = None        # (x, y, give up at)
        self.warping = None

    # -- devices --

    def scan(self):
        pattern = (self.config or {}).get("devices") or DEFAULT_DEVICES
        open_paths = set(d.path for d in self.devices.values())
        for name, path in list_devices(pattern):
            if path in open_paths or name == self.out_name:
                continue
            try:
                # Read-write: the daemon replays pointer signals into it.
                fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
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
        if grab == self.want_grab:
            return  # (devices found since are grabbed by scan())
        log("remote %s" % ("taken over for Kodi" if grab else "released to the TV"))
        self.want_grab = grab
        for dev in list(self.devices.values()):
            if grab and dev.cursor:
                self.inject(dev, CURSOR_HIDE)  # the TV's pointer goes before Kodi's shows
            self.set_grab(dev, grab)
            if not grab and dev.cursor:
                # The remote woke while taken over: the TV missed its "show".
                self.inject(dev, CURSOR_SHOW)
        if not grab and self.pointer and self.pos_seen:
            self.warp(self.pos[0], self.pos[1], HANDBACK_TIME)  # where Kodi's was

    def inject(self, dev, code):
        """Press and release `code` on the device itself, for the TV (not grabbed)."""
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1000000)
        dev.echoes.add(code)
        try:
            for value in (1, 0):
                os.write(dev.fd, struct.pack(EVENT, sec, usec, EV_KEY, code, value))
                os.write(dev.fd, struct.pack(EVENT, sec, usec, EV_SYN, SYN_REPORT, 0))
        except (IOError, OSError) as e:
            log("cannot replay %d on %s: %s" % (code, dev.name, e))

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
        if etype == EV_KEY and code in (CURSOR_SHOW, CURSOR_HIDE):
            # Not grabbed, the TV has it; grabbed, it's held back (and
            # replayed on the hand-back if the pointer is still awake).
            if code in dev.echoes:
                if not value:
                    dev.echoes.discard(code)
            elif value:
                dev.cursor = code == CURSOR_SHOW
            return
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
                self.pos_seen = True
            elif grabbed:
                self.resend(raw)
                dev.resent = True
        elif etype == EV_SYN and code == SYN_REPORT:
            if self.warp_to is not None:
                self.try_warp()
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

    def press(self, code):
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1000000)
        for value in (1, 0):
            self.resend(struct.pack(EVENT, sec, usec, EV_KEY, code, value))
            self.resend(struct.pack(EVENT, sec, usec, EV_SYN, SYN_REPORT, 0))
        log("pressed %d for the TV" % code)

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
            self.tv_sent = self.cursor_sent = None
            self.accepted_at = time.time()
            self.send_settings()
        if isinstance(msg.get("set_tv"), dict):
            self.set_settings(msg["set_tv"])
        if isinstance(msg.get("press"), int):
            self.press(msg["press"])
        if isinstance(msg.get("warp"), list) and len(msg["warp"]) == 2:
            try:
                self.warp(*msg["warp"])
            except (TypeError, ValueError):
                pass
        if "grab" in msg:
            self.consume = set(int(c) for c in msg.get("consume") or [])
            self.pointer = bool(msg.get("pointer"))
            self.apply_grab(bool(msg["grab"]) and self.accepted)

    # -- TV settings --

    def call(self, method, params, on_reply, subscribe=False):
        try:
            call = Luna(SETTINGS_URI + method if "://" not in method else method,
                        params, on_reply, subscribe)
        except (IOError, OSError) as e:
            log("luna-send failed: %s" % e)
            return None
        self.luna[call.fd] = call
        return call

    def watch_settings(self):
        """Keep a subscription per category; it delivers each change."""
        for category in sorted(set(TV_SETTINGS.values())):
            if self.watching.get(category) is not None:
                continue
            keys = sorted(k for k, c in TV_SETTINGS.items() if c == category)
            self.watching[category] = self.call(
                "getSystemSettings", {"category": category, "keys": keys, "subscribe": True},
                self.on_settings, subscribe=True)

    def on_settings(self, msg):
        settings = msg.get("settings")
        if not isinstance(settings, dict):
            return
        for key, value in settings.items():
            if key in TV_SETTINGS and isinstance(value, (str, type(u""))):
                self.tv[key] = value
        self.send_settings()

    def send_settings(self):
        if not self.accepted:
            return
        if not self.tv and time.time() - self.accepted_at < SETTINGS_WAIT:
            return  # the subscription's first reply is on its way
        if self.tv != self.tv_sent:
            self.tv_sent = dict(self.tv)
            self.send({"tv": self.tv})
        look = (self.tv.get("pointerSize"), self.tv.get("pointerShape"))
        if look != self.cursor_sent:
            self.cursor_sent = look
            images, hotspot = cursor_images(self.tv)
            if images:
                self.send({"cursor": images, "hotspot": hotspot})

    def set_settings(self, changes):
        by_category = {}
        for key, value in changes.items():
            if key in TV_SETTINGS and isinstance(value, (str, type(u""))):
                by_category.setdefault(TV_SETTINGS[key], {})[key] = value
        for category, settings in by_category.items():
            log("setting %s" % json.dumps(settings))
            self.call("setSystemSettings", {"category": category, "settings": settings},
                      lambda reply, s=settings: reply.get("returnValue") or
                      log("TV refused %s: %s" % (json.dumps(s), reply.get("errorText"))))

    # -- moving the TV's pointer --

    def warp(self, x, y, wait=WARP_TIME):
        self.warp_to = (int(x), int(y), time.time() + wait)
        self.try_warp()

    def try_warp(self):
        if self.warp_to is None or self.warping is not None:
            return
        x, y, until = self.warp_to
        if time.time() > until:
            self.warp_to = None
            return
        self.warping = self.call("luna://com.webos.service.mrcu/cursor/setPosition",
                                 {"posX": x, "posY": y}, self.on_warped)

    def on_warped(self, reply):
        self.warping = None
        if reply.get("returnValue"):
            if self.warp_to is not None:
                self.send({"warped": list(self.warp_to[:2])})
            self.warp_to = None
        elif self.warp_to is not None and time.time() > self.warp_to[2]:
            log("could not move the TV's pointer: %s" % reply.get("errorText"))
        # else: not ready (the remote waking up); the next motion retries

    def read_luna(self, call):
        if call.read():
            return
        self.luna.pop(call.fd, None)
        if call is self.warping:
            self.warping = None
        for category, sub in list(self.watching.items()):
            if sub is call:
                self.watching[category] = None  # resubscribed on the next scan

    # -- main loop --

    def stamp(self):
        try:
            st = os.stat(os.path.abspath(__file__))
            return "%d:%d" % (st.st_size, int(st.st_mtime))
        except OSError:
            return ""

    def run(self):
        log("Remote Bridge %s started (python %s)" % (VERSION, sys.version.split()[0]))
        try:
            self.loop()
        finally:
            for call in list(self.luna.values()):
                call.close()  # no orphaned subscriptions after a restart

    def loop(self):
        while True:
            now = time.time()
            if now >= self.next_scan:
                self.next_scan = now + RESCAN
                current = self.stamp()
                if current and current != self.self_stamp:
                    log("app updated, restarting")
                    return
                self.scan()
                self.watch_settings()
            if self.accepted and self.cursor_sent is None:
                self.send_settings()
            if self.sock is None and now >= self.next_connect:
                self.connect()
                if self.sock is None:
                    self.next_connect = now + RETRY
            if self.want_grab and now - self.last_heard > HEARTBEAT_TIMEOUT:
                log("no heartbeat from Kodi")
                self.apply_grab(False)
            fds = list(self.devices) + list(self.luna)
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
                elif fd in self.luna:
                    self.read_luna(self.luna[fd])
            self.flush_pos()


if __name__ == "__main__":
    Bridge().run()
