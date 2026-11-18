#!/usr/bin/env python3
"""
olde-tv: a channel-surfing video player for a Raspberry Pi feeding an
HDMI -> RF modulator.

* Plays full screen straight to the KMS/DRM framebuffer (no X / Wayland),
  using mpv as the decoder (hardware accelerated where possible).
* Forces the HDMI output to SD: 720x576@50 (PAL, default) or 720x480@60
  (NTSC, --standard ntsc) to match the RF modulator, and corrects the aspect ratio
  for the non-square pixels of a 4:3 TV.
* Every sub-directory of /video is a channel; the files in it play in order
  and loop forever.
* GPIO buttons change channel (hold a button to keep scanning).
* "live" schedule (default): channels keep "broadcasting" while you're away,
  like real TV. Each channel's position is derived from the wall clock, so
  tuning back in lands you wherever the programme has got to.
  "resume" schedule: each channel picks up exactly where you left it.

Signals: SIGUSR1 = channel up, SIGUSR2 = channel down.

Wiring (defaults, BCM numbering, buttons wired to ground, internal pull-ups):
    Channel up   -> GPIO17 (physical pin 11)  and GND (pin 9)
    Channel down -> GPIO27 (physical pin 13)  and GND (pin 14)

For testing over SSH you can also type  +  /  -  /  a channel number  and
press Enter on stdin.
"""

import argparse
import glob
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

log = logging.getLogger("olde-tv")

VIDEO_EXTS = {
    ".mp4", ".m4v", ".mkv", ".avi", ".mov", ".mpg", ".mpeg", ".ts", ".m2ts",
    ".webm", ".wmv", ".flv", ".vob", ".ogv", ".3gp", ".divx", ".xvid",
}

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "olde-tv")


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


# --------------------------------------------------------------------------
# Channels & durations
# --------------------------------------------------------------------------

def scan_channels(video_dir):
    """Return a list of (name, [files]) for each non-empty sub-directory."""
    channels = []
    try:
        entries = sorted(os.listdir(video_dir), key=natural_key)
    except OSError as e:
        log.error("cannot read %s: %s", video_dir, e)
        return channels
    for name in entries:
        d = os.path.join(video_dir, name)
        if not os.path.isdir(d) or name.startswith("."):
            continue
        files = []
        for root, dirs, fnames in os.walk(d, followlinks=True):
            dirs.sort(key=natural_key)
            for f in sorted(fnames, key=natural_key):
                if not f.startswith(".") and os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                    files.append(os.path.join(root, f))
        if files:
            channels.append((name, files))
        else:
            log.debug("skipping empty channel dir %s", d)
    return channels


class DurationCache:
    """ffprobe durations, cached on disk keyed by path+size+mtime."""

    def __init__(self):
        self.path = os.path.join(CACHE_DIR, "durations.json")
        self.lock = threading.Lock()
        self.data = {}
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            pass
        self.dirty = False

    @staticmethod
    def _key(path):
        try:
            st = os.stat(path)
        except OSError:
            return None
        return f"{path}|{st.st_size}|{int(st.st_mtime)}"

    def get(self, path):
        key = self._key(path)
        if key is None:
            return 0.0
        with self.lock:
            if key in self.data:
                return self.data[key]
        dur = 0.0
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", path],
                capture_output=True, text=True, timeout=30).stdout.strip()
            dur = float(out)
        except (subprocess.SubprocessError, ValueError, OSError):
            log.warning("could not get duration of %s", path)
        with self.lock:
            self.data[key] = dur
            self.dirty = True
        return dur

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
            self.dirty = False


# --------------------------------------------------------------------------
# Display / audio detection
# --------------------------------------------------------------------------

def find_hdmi_connector(preferred=None):
    """Return (card_path, connector_name, modes) for the HDMI output to use."""
    conns = []
    for p in sorted(glob.glob("/sys/class/drm/card*-HDMI-*")):
        card, conn = os.path.basename(p).split("-", 1)
        try:
            status = open(os.path.join(p, "status")).read().strip()
            modes = open(os.path.join(p, "modes")).read().split()
        except OSError:
            status, modes = "unknown", []
        conns.append((f"/dev/dri/{card}", conn, status, modes))
    if preferred:
        for c in conns:
            if c[1] == preferred:
                return c[0], c[1], c[3]
        log.warning("connector %s not found, auto-detecting", preferred)
    for c in conns:
        if c[2] == "connected":
            return c[0], c[1], c[3]
    if conns:  # nothing reports connected (e.g. no EDID) -> first HDMI port
        return conns[0][0], conns[0][1], conns[0][3]
    return None, None, []


def hdmi_audio_device(connector):
    # On the Pi 4, HDMI-A-1 <-> vc4hdmi0 and HDMI-A-2 <-> vc4hdmi1
    m = re.match(r"HDMI-A-(\d+)", connector or "")
    idx = int(m.group(1)) - 1 if m else 0
    return f"alsa/hdmi:CARD=vc4hdmi{idx},DEV=0"


# --------------------------------------------------------------------------
# Picture geometry
# --------------------------------------------------------------------------

def geometry_options(a):
    """mpv options that place the picture in the canvas box on the display.

    Display = the HDMI mode (a.width x a.height). Canvas = the box the TV
    actually shows (a.canvas_w x a.canvas_h at a.offset_x, a.offset_y), e.g.
    an NTSC-sized 720x480 box in the top left of a 720x576 PAL mode.
    """
    W, H = a.width, a.height
    cw, ch = a.canvas_w, a.canvas_h
    ox, oy = a.offset_x, a.offset_y
    ov = a.overscan / 100

    # pixel aspect (pixel width / height): the canvas covers the TV's whole
    # tv_aspect picture, e.g. 4:3 over 720x480 -> 0.889 (pixels are narrow)
    num, den = (float(v) for v in a.tv_aspect.replace("/", ":").split(":"))
    par = (num / den) / (cw / ch)

    # video area inside the canvas after shrinking for overscan
    left = ox + cw * ov
    top = oy + ch * ov
    vw = cw * (1 - 2 * ov)
    vh = ch * (1 - 2 * ov)
    # the box keeps its size; if it would stick out past a display edge, it
    # is clamped inside and the rest of the move is done by panning the
    # picture (the part past the edge is then simply cut off)
    def place(pos, size, total):
        clamped = min(max(pos, 0), max(total - size, 0))
        return clamped, (pos - clamped) / size
    left, pan_x = place(left, vw, W)
    top, pan_y = place(top, vh, H)
    right = max(W - left - vw, 0)
    bottom = max(H - top - vh, 0)

    # OSD margins are in units scaled to a 720-line window
    k = 720 / H
    osd_right = max(W - ox - cw, 0) + cw * ov + 60
    osd_top = max(oy, 0) + ch * ov + 40

    opts = [
        "--monitorpixelaspect=%.5f" % par,
        "--video-margin-ratio-left=%.5f" % (left / W),
        "--video-margin-ratio-right=%.5f" % (right / W),
        "--video-margin-ratio-top=%.5f" % (top / H),
        "--video-margin-ratio-bottom=%.5f" % (bottom / H),
        "--osd-margin-x=%d" % round(osd_right * k),
        "--osd-margin-y=%d" % round(osd_top * k),
    ]
    if pan_x or pan_y:
        opts += ["--video-pan-x=%.5f" % pan_x, "--video-pan-y=%.5f" % pan_y]
    return opts


# --------------------------------------------------------------------------
# mpv control over JSON IPC
# --------------------------------------------------------------------------

class MPV:
    def __init__(self, args, drm_device, connector):
        self.args = args
        self.sock_path = os.path.join(tempfile.gettempdir(), f"olde-tv-{os.getpid()}.sock")
        self.drm_device = drm_device
        self.connector = connector
        self.proc = None
        self.sock = None
        self.buf = b""
        self.req_id = 0
        self.lock = threading.Lock()

    def start(self):
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        a = self.args
        cmd = [
            "mpv",
            "--idle=yes",
            "--no-terminal",
            "--input-ipc-server=" + self.sock_path,
            "--no-input-default-bindings",
            "--input-vo-keyboard=no",
            "--vo=" + a.vo,
            "--drm-mode=" + a.resolution + (f"@{a.refresh}" if a.refresh else ""),
            "--fs",
            "--keepaspect=yes",
            "--hwdec=" + a.hwdec,
            "--background-color=#000000",
            "--osd-level=1",
            "--osd-font-size=" + str(a.osd_size),
            "--osd-color=#33FF33",
            "--osd-border-size=3",
            "--osd-align-x=right",
            "--osd-align-y=top",
            "--osd-bar=no",
            "--cursor-autohide=always",
            "--audio-device=" + a.audio_device,
            "--volume=" + str(a.volume),
            "--msg-level=all=warn",
        ]
        cmd += geometry_options(a)
        if a.vo in ("gpu", "gpu-next"):
            cmd.append("--gpu-context=drm")
        if self.drm_device:
            cmd.append("--drm-device=" + self.drm_device)
        if self.connector:
            cmd.append("--drm-connector=" + self.connector)
        if a.mpv_log:
            cmd.append("--log-file=" + a.mpv_log)
        cmd += a.mpv_arg
        log.debug("starting: %s", " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        for _ in range(100):
            if self.proc.poll() is not None:
                raise RuntimeError(f"mpv exited with code {self.proc.returncode}")
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.sock_path)
                self.sock = s
                self.buf = b""
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("could not connect to mpv IPC socket")
        self.sock.settimeout(5)
        self.command("disable_event", "all")

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def command(self, *cmd):
        with self.lock:
            self.req_id += 1
            rid = self.req_id
            msg = json.dumps({"command": list(cmd), "request_id": rid}) + "\n"
            self.sock.sendall(msg.encode())
            while True:
                while b"\n" not in self.buf:
                    chunk = self.sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("mpv closed the IPC socket")
                    self.buf += chunk
                line, self.buf = self.buf.split(b"\n", 1)
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                if reply.get("request_id") == rid:
                    if reply.get("error") not in ("success", None):
                        log.debug("mpv %s -> %s", cmd[0], reply.get("error"))
                    return reply.get("data")

    def get(self, prop):
        return self.command("get_property", prop)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.command("quit")
                self.proc.wait(3)
            except Exception:
                self.proc.terminate()
                try:
                    self.proc.wait(3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass


# --------------------------------------------------------------------------
# The TV
# --------------------------------------------------------------------------

class TV:
    def __init__(self, args, mpv, durations):
        self.args = args
        self.mpv = mpv
        self.durations = durations
        self.lock = threading.RLock()
        self.channels = scan_channels(args.video_dir)
        self.current = None           # index into self.channels
        self.resume = {}              # channel name -> (file index, position)
        self.state_file = os.path.join(CACHE_DIR, "state.json")
        self.last_name = None
        try:
            with open(self.state_file) as f:
                st = json.load(f)
                self.last_name = st.get("channel")
                self.resume = {k: tuple(v) for k, v in st.get("resume", {}).items()}
        except (OSError, ValueError):
            pass

    # -- schedule ---------------------------------------------------------

    def live_position(self, files):
        """Where the channel 'is' right now if it had been playing since 1970."""
        durs = [self.durations.get(f) for f in files]
        total = sum(durs)
        if total <= 0:
            return 0, 0.0
        t = time.time() % total
        for i, d in enumerate(durs):
            if t < d:
                return i, t
            t -= d
        return 0, 0.0

    def save_current_position(self):
        if self.current is None or not self.mpv.alive():
            return
        name, files = self.channels[self.current]
        path = self.mpv.get("path")
        pos = self.mpv.get("time-pos") or 0.0
        if path in files:
            self.resume[name] = (files.index(path), float(pos))

    def save_state(self):
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            name = self.channels[self.current][0] if self.current is not None else None
            with open(self.state_file, "w") as f:
                json.dump({"channel": name, "resume": self.resume}, f)
        except OSError as e:
            log.warning("could not save state: %s", e)
        self.durations.save()

    # -- tuning -----------------------------------------------------------

    def tune(self, index, show_osd=True):
        with self.lock:
            if self.args.schedule == "resume":
                self.save_current_position()
            # rescan so newly added channels / files show up
            old_name = self.channels[self.current][0] if self.current is not None else None
            self.channels = scan_channels(self.args.video_dir)
            if not self.channels:
                log.warning("no channels found in %s", self.args.video_dir)
                self.current = None
                self.mpv.command("stop")
                return
            index %= len(self.channels)
            self.current = index
            name, files = self.channels[index]

            if self.args.schedule == "live":
                start_i, start_pos = self.live_position(files)
            else:
                start_i, start_pos = self.resume.get(name, (0, 0.0))
                if start_i >= len(files):
                    start_i, start_pos = 0, 0.0

            order = files[start_i:] + files[:start_i]
            log.info("CH %d %s: %s @ %.1fs", index + 1, name,
                     os.path.basename(order[0]), start_pos)

            # first file with a start offset, the rest of the loop appended.
            # When the playlist runs out the watchdog retunes (keeps it looping).
            opts = f"start={start_pos:.2f}" if start_pos > 1 else ""
            self.mpv.command("loadfile", order[0], "replace", -1, opts)
            for f in order[1:]:
                self.mpv.command("loadfile", f, "append")

            if show_osd:
                label = f"{index + 1:02d}"
                if self.args.osd_names:
                    label += f"\n{name}"
                self.mpv.command("show-text", label, int(self.args.osd_time * 1000))
            if old_name != name:
                self.save_state()

    def step(self, delta):
        with self.lock:
            if self.current is None:
                self.tune(0)
            else:
                self.tune(self.current + delta)

    def goto(self, number):
        with self.lock:
            if 1 <= number <= len(self.channels):
                self.tune(number - 1)

    def start(self):
        idx = 0
        for i, (name, _) in enumerate(self.channels):
            if name == self.last_name:
                idx = i
        self.tune(idx)

    def watchdog_tick(self):
        """Called periodically: keep the channel looping."""
        with self.lock:
            if self.current is None:
                if scan_channels(self.args.video_dir):
                    self.tune(0)
                return
            if self.mpv.get("idle-active"):
                name, files = self.channels[self.current]
                if self.args.schedule == "resume":
                    self.resume[name] = (0, 0.0)
                self.tune(self.current, show_osd=False)


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def setup_gpio(args, tv):
    try:
        from gpiozero import Button
    except ImportError:
        log.error("gpiozero not installed: sudo apt install python3-gpiozero")
        return []

    buttons = []

    def make(pin, delta, label):
        try:
            b = Button(pin, pull_up=not args.active_high, bounce_time=args.debounce,
                       hold_time=args.hold_time, hold_repeat=True)
        except Exception as e:
            log.error("cannot set up GPIO%d for %s: %s", pin, label, e)
            return
        b.when_pressed = lambda: safe(tv.step, delta)
        b.when_held = lambda: safe(tv.step, delta)
        buttons.append(b)
        log.info("%s button on GPIO%d", label, pin)

    make(args.up_pin, +1, "channel up")
    make(args.down_pin, -1, "channel down")
    return buttons


def safe(fn, *a):
    try:
        fn(*a)
    except Exception:
        log.exception("error handling input")


def stdin_reader(tv):
    if not sys.stdin or not sys.stdin.isatty():
        return
    print("keys: + / - / <channel number>, then Enter.  Ctrl-C quits.", flush=True)
    for line in sys.stdin:
        s = line.strip()
        if s in ("+", "u", "up", "="):
            safe(tv.step, +1)
        elif s in ("-", "d", "down"):
            safe(tv.step, -1)
        elif s.isdigit():
            safe(tv.goto, int(s))


def prewarm(tv):
    for _, files in list(tv.channels):
        for f in files:
            tv.durations.get(f)
    tv.durations.save()
    log.info("durations cached")


# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Olde TV channel player")
    p.add_argument("--video-dir", default="/video")
    p.add_argument("--up-pin", type=int, default=17, help="BCM pin for channel up")
    p.add_argument("--down-pin", type=int, default=27, help="BCM pin for channel down")
    p.add_argument("--active-high", action="store_true",
                   help="buttons pull the pin to 3.3V instead of ground")
    p.add_argument("--debounce", type=float, default=0.05)
    p.add_argument("--hold-time", type=float, default=0.6,
                   help="seconds a button must be held before it auto-repeats")
    p.add_argument("--schedule", choices=["live", "resume"], default="live")
    p.add_argument("--standard", choices=["pal", "ntsc"], default="pal",
                   help="TV standard the RF modulator outputs: pal = 720x576@50, "
                        "ntsc = 720x480@60")
    p.add_argument("--resolution", default=None, help="override, e.g. 720x576")
    p.add_argument("--refresh", default=None, help="override, e.g. 50, 60 or 59.94")
    p.add_argument("--overscan", type=float, default=0,
                   help="shrink the picture by this %% on each side if the TV "
                        "crops the edges (try 3-8)")
    p.add_argument("--tv-aspect", default="4:3", help="4:3 or 16:9 for widescreen sets")
    p.add_argument("--canvas", default=None,
                   help="draw the picture in a WxH box instead of the full display, "
                        "e.g. 720x480 to show NTSC-sized video on a PAL output")
    p.add_argument("--offset-x", type=int, default=0,
                   help="move the picture right by this many pixels (negative = left)")
    p.add_argument("--offset-y", type=int, default=0,
                   help="move the picture down by this many pixels (negative = up)")
    p.add_argument("--connector", default=None, help="DRM connector, e.g. HDMI-A-1 (default: auto)")
    p.add_argument("--audio-device", default=None, help="mpv audio device (default: matching HDMI)")
    p.add_argument("--volume", type=int, default=100)
    p.add_argument("--vo", default="gpu", choices=["gpu", "gpu-next", "drm"],
                   help="gpu = OpenGL on DRM (fast); drm = plain dumb framebuffer")
    p.add_argument("--hwdec", default="v4l2m2m-copy",
                   help="Pi 4 hardware decode; falls back to software if unsupported")
    p.add_argument("--osd-size", type=int, default=64)
    p.add_argument("--osd-time", type=float, default=3.0)
    p.add_argument("--osd-names", action="store_true", help="show directory name under number")
    p.add_argument("--mpv-log", default=None, help="write a full mpv log here")
    p.add_argument("--mpv-arg", action="append", default=[], help="extra raw mpv option")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    std_res, std_hz = {"pal": ("720x576", "50"), "ntsc": ("720x480", "60")}[args.standard]
    args.resolution = args.resolution or std_res
    args.refresh = std_hz if args.refresh is None else args.refresh
    args.width, args.height = (int(v) for v in args.resolution.split("x"))
    try:
        args.canvas_w, args.canvas_h = (
            (int(v) for v in args.canvas.lower().split("x")) if args.canvas
            else (args.width, args.height))
    except ValueError:
        p.error("--canvas must look like 720x480")
    log.info("standard %s: %s@%sHz, picture %dx%d at %+d,%+d, overscan %g%%",
             args.standard.upper(), args.resolution, args.refresh or "any",
             args.canvas_w, args.canvas_h, args.offset_x, args.offset_y, args.overscan)

    drm_device, connector, modes = find_hdmi_connector(args.connector)
    if connector is None:
        log.error("no HDMI connector found")
        return 1
    log.info("output %s on %s", connector, drm_device)
    if modes and args.resolution not in modes:
        log.warning("%s not offered by %s (modes: %s). mpv may fail; force it with "
                    "video=%s:%sD on the kernel cmdline (see README).",
                    args.resolution, connector, " ".join(sorted(set(modes))),
                    connector, args.resolution)
    if args.audio_device is None:
        args.audio_device = hdmi_audio_device(connector)

    durations = DurationCache()
    mpv = MPV(args, drm_device, connector)
    mpv.start()
    tv = TV(args, mpv, durations)

    # plain flag: a signal handler must not take locks (Event.set() can deadlock
    # if a second signal arrives while the first handler is running)
    stop = {"now": False}

    def on_signal(signum, frame):
        stop["now"] = True
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    # `pkill -USR1 -f olde_tv.py` = channel up, USR2 = channel down
    signal.signal(signal.SIGUSR1, lambda s, f: threading.Thread(
        target=safe, args=(tv.step, +1), daemon=True).start())
    signal.signal(signal.SIGUSR2, lambda s, f: threading.Thread(
        target=safe, args=(tv.step, -1), daemon=True).start())

    if args.schedule == "live":
        threading.Thread(target=prewarm, args=(tv,), daemon=True).start()
    tv.start()
    buttons = setup_gpio(args, tv)
    threading.Thread(target=stdin_reader, args=(tv,), daemon=True).start()

    try:
        while not stop["now"]:
            time.sleep(0.5)
            if stop["now"]:
                break
            try:
                if not mpv.alive():
                    log.warning("mpv died (code %s), restarting", mpv.proc.returncode)
                    time.sleep(1)
                    mpv.start()
                    with tv.lock:
                        tv.tune(tv.current or 0)
                    continue
                tv.watchdog_tick()
            except Exception:
                log.exception("watchdog error")
                time.sleep(2)
    finally:
        with tv.lock:
            if args.schedule == "resume":
                try:
                    tv.save_current_position()
                except Exception:
                    pass
            tv.save_state()
        for b in buttons:
            b.close()
        mpv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
