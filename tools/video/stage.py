"""The stage of the demo video: virtual screens, browser windows, a terminal,
the recordings and the camera.

make_video.py tells the story, this module films it. Every window lives on a
screen of its own (Xvfb), so nothing opens on your desktop. Chrome runs headed
in kiosk mode, because only a headed browser paints like the real thing, and
ffmpeg records each screen with x11grab. The camera works afterwards on the
recording, which is larger than the video, so it can move closer without
blurring the text.
"""

import base64
import fcntl
import json
import math
import os
import random
import re
import signal
import struct
import subprocess
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
XTERM = HERE / "node_modules" / "@xterm" / "xterm"

FPS = 30
# The browser lays pages out at 1536x864 CSS pixels, a common laptop size, and
# paints them at 5/3 the density. The video is 1920x1080, so the camera can come
# up to 4/3 closer before it has to enlarge anything.
CSS_W, CSS_H = 1536, 864
REC_W, REC_H = 2560, 1440
DPR = REC_W / CSS_W
OUT_W, OUT_H = 1920, 1080

# A clean environment for everything on a virtual screen: no Wayland, no
# session bus and no desktop scale factor from the real session.
BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "en_US.UTF-8",
    "TZ": "Europe/Berlin",
}


class Screen:
    """A virtual X screen of its own."""

    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        read, write = os.pipe()
        self.proc = subprocess.Popen(
            ["Xvfb", "-displayfd", str(write), "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"],
            pass_fds=(write,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.close(write)
        number = b""
        with os.fdopen(read, "rb") as f:
            while not number.endswith(b"\n"):
                chunk = f.read(1)
                if not chunk:
                    raise RuntimeError("Xvfb did not start")
                number += chunk
        self.display = f":{number.decode().strip()}"

    def env(self, home: Path) -> dict:
        return {**BASE_ENV, "DISPLAY": self.display, "HOME": str(home)}

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# A pointer for the viewer. Playwright's mouse never moves the X pointer, so the
# page draws its own, follows every mousemove and shows a ring on a click. It is
# a popover, so it sits in the top layer above open dialogs, and it raises itself
# again whenever a dialog opens.
POINTER_JS = r"""
(() => {
  if (window.__tourPointer) return;
  window.__tourPointer = true;
  const install = () => {
    const p = document.createElement("div");
    p.setAttribute("popover", "manual");
    p.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24" style="display:block;filter:drop-shadow(0 1px 2px rgba(0,0,0,.35))">'
      + '<path d="M5 2.5v17.2l4.3-4.1 2.9 6.6 2.9-1.3-2.9-6.5 5.9-.4z" fill="#1f2328" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    Object.assign(p.style, {
      position: "fixed", inset: "auto", left: "0", top: "0", margin: "0", padding: "0", border: "0",
      background: "transparent", overflow: "visible", pointerEvents: "none",
      transform: "translate(-200px, -200px)", width: "24px", height: "24px",
    });
    document.documentElement.appendChild(p);
    const raise = () => {
      try { if (p.matches(":popover-open")) p.hidePopover(); p.showPopover(); } catch (e) {}
    };
    raise();
    let x = -200, y = -200;
    addEventListener("mousemove", (e) => {
      x = e.clientX; y = e.clientY;
      p.style.transform = `translate(${x - 5}px, ${y - 3}px)`;
    }, true);
    addEventListener("mousedown", (e) => {
      const ring = document.createElement("div");
      ring.setAttribute("popover", "manual");
      Object.assign(ring.style, {
        position: "fixed", inset: "auto", left: `${e.clientX - 18}px`, top: `${e.clientY - 18}px`,
        width: "36px", height: "36px", margin: "0", padding: "0", borderRadius: "50%",
        border: "2px solid rgba(9,105,218,.7)", background: "rgba(9,105,218,.12)", pointerEvents: "none",
        transition: "transform .45s ease-out, opacity .45s ease-out", transform: "scale(.4)", opacity: "1",
      });
      document.documentElement.appendChild(ring);
      try { ring.showPopover(); } catch (e) {}
      raise();
      requestAnimationFrame(() => requestAnimationFrame(() => { ring.style.transform = "scale(1.3)"; ring.style.opacity = "0"; }));
      setTimeout(() => ring.remove(), 600);
    }, true);
    new MutationObserver((records) => {
      if (records.some((r) => r.target.tagName === "DIALOG" || [...r.addedNodes].some((n) => n.tagName === "DIALOG"))) raise();
    }).observe(document.documentElement, { subtree: true, childList: true, attributes: true, attributeFilter: ["open"] });
  };
  if (document.documentElement) install(); else addEventListener("DOMContentLoaded", install);
})();
"""


def ease(t: float) -> float:
    """Ease in and out, 0 to 1."""
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


class Window:
    """A Chrome window that fills its screen, in kiosk mode."""

    def __init__(self, pw, screen: Screen, profile: Path, home: Path, scale: float = DPR, pointer: bool = True):
        self.screen = screen
        args = [
            "--ozone-platform=x11",
            "--kiosk",
            "--window-position=0,0",
            f"--window-size={round(screen.width / scale)},{round(screen.height / scale)}",
            f"--force-device-scale-factor={scale}",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            "--disable-features=Translate,MediaRouter",
            "--password-store=basic",
        ]
        self.ctx = pw.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=False, env=screen.env(home), no_viewport=True,
            timezone_id="Europe/Berlin", locale="en-US", args=args,
        )
        if pointer:
            self.ctx.add_init_script(POINTER_JS)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.mouse = (CSS_W * 0.62, CSS_H * 0.72)

    def sign_in(self, base: str, session: str):
        self.ctx.clear_cookies()
        self.ctx.add_cookies([{"name": "meertex_session", "value": session, "url": base}])

    # --- the pointer ---------------------------------------------------------

    def glide(self, x: float, y: float, seconds: float = 0.7):
        """Move the pointer to x, y (CSS pixels) along an eased path."""
        x0, y0 = self.mouse
        steps = max(2, round(seconds * 50))
        start = time.monotonic()
        for i in range(1, steps + 1):
            e = ease(i / steps)
            self.page.mouse.move(x0 + (x - x0) * e, y0 + (y - y0) * e)
            wait = start + seconds * i / steps - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        self.mouse = (x, y)

    def point(self, locator, seconds: float = 0.7, dx: float = 0.5, dy: float = 0.5):
        """Move the pointer onto a locator, dx and dy into its box."""
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        self.glide(box["x"] + box["width"] * dx, box["y"] + box["height"] * dy, seconds)

    def click(self, locator, seconds: float = 0.7, pause: float = 0.18):
        self.point(locator, seconds)
        time.sleep(pause)
        self.page.mouse.down()
        time.sleep(0.07)
        self.page.mouse.up()

    def park(self):
        """Put the pointer back where it was, so it shows on the first frame."""
        self.page.mouse.move(*self.mouse)

    def close(self):
        self.ctx.close()


class Terminal:
    """bash in a pseudo terminal, shown with xterm.js in a Window.

    stage.py serves terminal.html, xterm.js and the terminal's output as
    server-sent events. Input goes straight to the pseudo terminal, typed at a
    human pace."""

    def __init__(self, window: Window, cwd: Path, env: dict, rcfile: Path, port: int, title: str = "Terminal"):
        self.window = window
        self.output = bytearray()
        self.cond = threading.Condition()
        self.events: list[tuple[str, str]] = []
        self.rng = random.Random(7)
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

        page = window.page
        page.goto(f"http://127.0.0.1:{port}/terminal.html")
        page.wait_for_function("window.termReady === true")
        size = page.evaluate("window.termSize")
        self.cols, self.rows = size["cols"], size["rows"]
        self.set_title(title)

        self.master, slave = os.openpty()
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.cols, 0, 0))
        env = {**env, "TERM": "xterm-256color", "COLUMNS": str(self.cols), "LINES": str(self.rows)}
        # setsid -c makes the pseudo terminal bash's controlling terminal, so job
        # control and vim work as in a real one.
        self.proc = subprocess.Popen(
            ["setsid", "-c", "bash", "--noprofile", "--rcfile", str(rcfile), "-i"],
            stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env, close_fds=True,
        )
        os.close(slave)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                data = b""
            with self.cond:
                if not data:
                    self.events.append(("end", ""))
                    self.cond.notify_all()
                    return
                self.output.extend(data)
                self.events.append(("out", base64.b64encode(data).decode()))
                self.cond.notify_all()

    def _handler(self):
        term = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                files = {
                    "/terminal.html": (HERE / "terminal.html", "text/html; charset=utf-8"),
                    "/xterm.js": (XTERM / "lib" / "xterm.js", "text/javascript"),
                    "/xterm.css": (XTERM / "css" / "xterm.css", "text/css"),
                }
                if self.path in files:
                    path, kind = files[self.path]
                    body = path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path != "/stream":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                sent = 0
                try:
                    while True:
                        with term.cond:
                            while sent == len(term.events):
                                term.cond.wait(timeout=15)
                                if sent == len(term.events):
                                    self.wfile.write(b": ping\n\n")
                                    self.wfile.flush()
                            batch = term.events[sent:]
                            sent = len(term.events)
                        for kind, data in batch:
                            if kind == "end":
                                return
                            self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

        return Handler

    def set_title(self, title: str):
        with self.cond:
            self.events.append(("title", title))
            self.cond.notify_all()

    def text(self) -> str:
        with self.cond:
            return self.output.decode("utf-8", "replace")

    def send(self, keys: str):
        os.write(self.master, keys.encode())

    def type(self, text: str, cps: float = 16.0):
        """Type text at about cps characters a second, a little uneven like a person."""
        for ch in text:
            os.write(self.master, ch.encode())
            pause = 1.0 / cps * self.rng.uniform(0.6, 1.5)
            if ch == " ":
                pause *= 1.3
            time.sleep(pause)

    def type_for(self, text: str, seconds: float):
        """Type text so that it takes about seconds."""
        self.type(text, cps=len(text) / seconds * 1.08)

    def wait_for(self, pattern: str, since: int = 0, timeout: float = 20.0) -> str:
        end = time.monotonic() + timeout
        rx = re.compile(pattern)
        while time.monotonic() < end:
            with self.cond:
                text = self.output[since:].decode("utf-8", "replace")
            if rx.search(text):
                return text
            time.sleep(0.05)
        raise RuntimeError(f"the terminal did not show {pattern!r} within {timeout:.0f} s")

    def mark(self) -> int:
        with self.cond:
            return len(self.output)

    def run(self, command: str, done: str, timeout: float = 30.0):
        """Run a command off camera and wait until its output contains done."""
        since = self.mark()
        self.send(command + "\r")
        self.wait_for(done, since, timeout)

    def stop(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGHUP)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.server.shutdown()
        try:
            os.close(self.master)
        except OSError:
            pass


class Recording:
    """ffmpeg records a screen losslessly. It notes when its first frame was
    taken, so the clip can be cut at the moment the action started."""

    def __init__(self, screen: Screen, path: Path, seconds: float):
        self.path = path
        self.log = path.with_suffix(".log")
        with open(self.log, "w") as log:
            self.proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-y", "-f", "x11grab", "-draw_mouse", "0", "-framerate", str(FPS),
                 "-video_size", f"{screen.width}x{screen.height}", "-i", screen.display, "-t", f"{seconds:.3f}",
                 "-c:v", "utvideo", "-pix_fmt", "gbrp", str(path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
            )

    def wait(self):
        self.proc.wait()
        if self.proc.returncode:
            raise RuntimeError(f"ffmpeg failed to record, see {self.log}")

    def started(self) -> float:
        """The wall-clock time of the first frame (x11grab stamps frames with it)."""
        for _ in range(100):
            m = re.search(r"start: ([0-9.]+)", self.log.read_text())
            if m:
                return float(m.group(1))
            time.sleep(0.05)
        raise RuntimeError(f"no start time in {self.log}")


# --- the camera -------------------------------------------------------------------


class Camera:
    """Where the camera looks over time: keyframes (seconds, zoom, (x, y)) with
    the centre in the page's CSS pixels. Between two keyframes it eases. A zoom
    of 1 shows the whole screen, 4/3 is as close as it gets without enlarging."""

    def __init__(self, keys):
        self.keys = sorted(keys, key=lambda k: k[0])

    @classmethod
    def still(cls, zoom: float = 1.0, at=(CSS_W / 2, CSS_H / 2)):
        return cls([(0.0, zoom, at)])

    def at(self, t: float):
        keys = self.keys
        if t <= keys[0][0]:
            return keys[0][1], keys[0][2]
        for (t0, z0, c0), (t1, z1, c1) in zip(keys, keys[1:]):
            if t <= t1:
                e = ease((t - t0) / (t1 - t0)) if t1 > t0 else 1.0
                # Zoom in log space, so in and out feel the same speed.
                z = math.exp(math.log(z0) + (math.log(z1) - math.log(z0)) * e)
                return z, (c0[0] + (c1[0] - c0[0]) * e, c0[1] + (c1[1] - c0[1]) * e)
        return keys[-1][1], keys[-1][2]

    def box(self, t: float):
        """The part of the recording (in recorded pixels) that fills the frame at t."""
        z, (cx, cy) = self.at(t)
        w, h = REC_W / z, REC_H / z
        x = min(max(cx * DPR - w / 2, 0.0), REC_W - w)
        y = min(max(cy * DPR - h / 2, 0.0), REC_H - h)
        return (x, y, x + w, y + h)


def window_mask(path: Path, w: int, h: int, radius: int):
    """A white rounded rectangle, the shape of the terminal window."""
    subprocess.run(["magick", "-size", f"{w}x{h}", "xc:black", "-fill", "white",
                    "-draw", f"roundrectangle 0,0 {w - 1},{h - 1} {radius},{radius}", str(path)], check=True)


def window_shadow(path: Path, w: int, h: int, radius: int, pad: int):
    """A soft shadow under the terminal window, pad pixels larger on every side."""
    subprocess.run(["magick", "-size", f"{w + 2 * pad}x{h + 2 * pad}", "xc:none", "-fill", "rgba(0,0,0,0.42)",
                    "-draw", f"roundrectangle {pad},{pad + pad // 4} {pad + w - 1},{pad + h - 1 + pad // 4} {radius},{radius}",
                    "-blur", f"0x{pad // 2.5}", str(path)], check=True)


def develop(main: Path, main_skip: float, frames: int, camera: Camera, out: Path, still: Path | None = None,
            still_at: int = 0, term: Path | None = None, term_skip: float = 0.0, term_at=(0, 0), term_size=(0, 0),
            mask: Path | None = None, shadow: Path | None = None, shadow_pad: int = 0):
    """Turn the recordings of a shot into its clip: cut them at the start of the
    action, put the terminal window over the browser, move the camera and scale
    to the size of the video. The clip is lossless (FFV1), and frame still_at is
    saved as a PNG to still."""
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{main_skip:.3f}", "-i", str(main)]
    if term:
        tw, th = term_size
        tx, ty = term_at
        cmd += ["-ss", f"{term_skip:.3f}", "-i", str(term), "-loop", "1", "-i", str(mask), "-loop", "1", "-i", str(shadow)]
        graph = (
            f"[0:v]format=rgb24[bg];"
            f"[1:v]format=gbrap[t0];[2:v]format=gray,scale={tw}:{th}[m];[t0][m]alphamerge[t];"
            f"[3:v]format=rgba[s];"
            f"[bg][s]overlay={tx - shadow_pad}:{ty - shadow_pad}:shortest=1[b1];"
            f"[b1][t]overlay={tx}:{ty}:shortest=1,format=rgb24[v]"
        )
        cmd += ["-filter_complex", graph, "-map", "[v]"]
    else:
        cmd += ["-vf", "format=rgb24"]
    cmd += ["-frames:v", str(frames), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    reader = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    writer = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{OUT_W}x{OUT_H}",
         "-framerate", str(FPS), "-i", "-", "-c:v", "ffv1", "-level", "3", "-pix_fmt", "yuv444p", str(out)],
        stdin=subprocess.PIPE,
    )
    size = REC_W * REC_H * 3
    last = None
    for n in range(frames):
        data = reader.stdout.read(size)
        if len(data) < size:
            # The recording ran a little short. Hold its last frame.
            if last is None:
                raise RuntimeError(f"{main} has no frames")
            frame = last
        else:
            frame = Image.frombuffer("RGB", (REC_W, REC_H), data, "raw", "RGB", 0, 1)
            last = frame
        shot = frame.resize((OUT_W, OUT_H), Image.LANCZOS, box=camera.box(n / FPS))
        writer.stdin.write(shot.tobytes())
        if still and n == still_at:
            shot.save(still)
    reader.stdout.close()
    reader.wait()
    writer.stdin.close()
    if writer.wait():
        raise RuntimeError(f"ffmpeg failed to write {out}")


def write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False))
