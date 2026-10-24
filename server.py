"""Mic Control: pro mic + guitar processing for every app, through Equalizer APO.

Equalizer APO runs inside the Windows audio engine, so once it is attached to the Focusrite's input,
whatever it is told to do happens before any app (Discord, OBS, games, browsers) gets the audio.
This server keeps the settings, writes Equalizer APO's config (see chain.py) and serves the app.
"""

import copy
import ctypes
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import winreg
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import chain

APP_DIR = Path(__file__).resolve().parent
# MICCONTROL_HOME / MICCONTROL_APO_CONFIG let a test copy run against throwaway folders instead of the
# real settings and the real Equalizer APO config (the live microphone).
DATA_DIR = Path(os.environ.get("MICCONTROL_HOME") or APP_DIR)
APO_CONFIG_OVERRIDE = os.environ.get("MICCONTROL_APO_CONFIG")
STATE_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "mic-control.log"
CODE_FILES = (Path(__file__).resolve(), APP_DIR / "index.html", APP_DIR / "chain.py")
PORT = 47820
URL = f"http://127.0.0.1:{PORT}/"
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
STATIC = {"/fonts/": APP_DIR / "fonts"}
MIME = {".woff2": "font/woff2", ".ttf": "font/ttf", ".svg": "image/svg+xml", ".png": "image/png"}

HKLM = winreg.HKEY_LOCAL_MACHINE
APO_KEY = r"SOFTWARE\EqualizerAPO"
CAPTURE_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
PKEY_ENDPOINT_NAME = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
PKEY_ADAPTER_NAME = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
PKEY_DEVICE_FORMAT = "{f19f064d-082c-4e27-bc73-6882a1bb8e4c},0"  # shared-mode WAVEFORMATEX(TENSIBLE) blob

# config.txt gets two managed blocks: one before the user's own lines that keeps them (e.g. a Peace
# headphone EQ set to "Device: all") off the mic, and one after them that pulls in the mic chain.
MARK_BEGIN = "# >>> Mic Control"
MARK_END = "# <<< Mic Control"
MIC_SUBDIR = "MicControl"
INCLUDE_LINE = rf"Include: {MIC_SUBDIR}\mic.txt"
MAX_PRESETS = 60
MAX_BODY = 262144  # the biggest real request (8 effects with every control) is ~20 KB
RELOAD_GAP = 0.3  # seconds between Equalizer APO reloads, at least
MONITOR_EXE = APP_DIR / "monitor" / "bin" / "MicMonitor.exe"
TRAY_EXE = APP_DIR / "tray" / "bin" / "MicTray.exe"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"  # per-user "start with Windows" list
RUN_NAME = "MicControl"
CRASH_CHECK_EVERY = 15  # seconds between looks at the Windows crash log
LOCK_STUCK_AFTER = 20   # seconds the settings may be held before the engine counts as hung and restarts
MONITOR_MODES = ("auto", "exclusive", "shared")
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
MONITOR_ERRORS = {  # Windows audio errors the monitor can hit, in words
    "0x8007054F": "Windows can't open the Focusrite right now (its driver is stuck). Unplug it and plug it back in, or restart the PC.",
    "0x88890004": "The mic or output went away (unplugged or switched off). It reconnects by itself when it's back.",
    "0x8889000A": "Another app has the output to itself. Pick another output or close that app.",
    "0x88890008": "That output doesn't accept this format. Pick Shared mode or another output.",
}


def log(msg):
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:  # keep one older log, never grow forever
            os.replace(LOG_FILE, LOG_FILE.with_suffix(".old.log"))
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def write_atomic(path, text):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\r\n")
    # Equalizer APO re-reads its config the moment the folder changes, and while it has the file
    # open Windows refuses the replace. That lasts milliseconds, so retry for up to a second.
    for _ in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.025)
    os.replace(tmp, path)


def file_hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def code_version(path):
    """Fingerprint of one of the app's own files, to notice when it has been updated on disk."""
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return None


def key_mtime(subkey):
    """Last-write time of a registry key as a Unix timestamp, or None."""
    try:
        with winreg.OpenKey(HKLM, subkey) as k:
            filetime = winreg.QueryInfoKey(k)[2]
        return filetime / 10_000_000 - 11_644_473_600
    except OSError:
        return None


def boot_time():
    tick = ctypes.windll.kernel32.GetTickCount64
    tick.restype = ctypes.c_ulonglong
    return time.time() - tick() / 1000


def apo_paths():
    try:
        with winreg.OpenKey(HKLM, APO_KEY) as k:
            install = winreg.QueryValueEx(k, "InstallPath")[0]
            config = winreg.QueryValueEx(k, "ConfigPath")[0]
        return Path(install), Path(APO_CONFIG_OVERRIDE or config)
    except OSError:
        return None, None


def apo_device_ids():
    """Endpoint GUIDs that Equalizer APO's Device Selector has been attached to."""
    ids = set()
    try:
        with winreg.OpenKey(HKLM, APO_KEY + r"\Child APOs") as k:
            i = 0
            while True:
                try:
                    ids.add(winreg.EnumKey(k, i).lower())
                except OSError:
                    break
                i += 1
    except OSError:
        pass
    return ids


def capture_devices():
    attached = apo_device_ids()
    devices = []
    try:
        root = winreg.OpenKey(HKLM, CAPTURE_KEY)
    except OSError:
        return devices
    with root:
        i = 0
        while True:
            try:
                guid = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, guid) as dk:
                    state = winreg.QueryValueEx(dk, "DeviceState")[0]
                with winreg.OpenKey(root, guid + r"\Properties") as pk:
                    name = winreg.QueryValueEx(pk, PKEY_ENDPOINT_NAME)[0]
                    adapter = winreg.QueryValueEx(pk, PKEY_ADAPTER_NAME)[0]
                    try:
                        # 8-byte property header, then WAVEFORMATEX: tag, channels, sample rate
                        blob = winreg.QueryValueEx(pk, PKEY_DEVICE_FORMAT)[0]
                        channels, rate = struct.unpack_from("<HI", blob, 10)
                    except (OSError, TypeError, struct.error):
                        channels, rate = None, None
            except OSError:
                continue
            if state != 1:  # 1 = active (plugged in and enabled)
                continue
            gid = guid.lower()
            devices.append({"id": gid, "name": name, "adapter": re.sub(r"^\d+-\s*", "", adapter),
                            "apo": gid in attached, "apoKey": rf"{APO_KEY}\Child APOs\{guid}",
                            "channels": channels, "rate": rate})
    devices.sort(key=lambda d: ("focusrite" not in d["adapter"].lower(), d["name"].lower()))
    return devices


def device_pattern(dev):
    if not dev:
        return "MicControl-no-device-selected"
    # Equalizer APO matches when every word appears in the device name + connection name.
    return f"{dev['adapter']} {dev['name']}".replace(";", " ")


def clamp_volume(v, fallback=1.0):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return fallback  # junk never jumps the volume up
    return min(1.5, max(0.0, float(v)))


class Monitor:
    """"Hear yourself": runs monitor/MicMonitor.exe, which plays the processed mic (what apps hear) to an
    output with the least delay Windows allows. It stops by itself if this process goes away (its stdin
    closes), and is restarted here if it ever dies while it should be on (e.g. a USB dropout)."""

    def __init__(self, saved):
        saved = saved if isinstance(saved, dict) else {}
        self.output = str(saved.get("output") or "default")[:200]
        self.volume = clamp_volume(saved.get("volume", 1.0))
        self.mode = saved.get("mode") if saved.get("mode") in MONITOR_MODES else "auto"
        self.on = False
        self.proc = None
        self.running_for = None   # (capture id, output id, exclusive) the running process was started with
        self.status = {}
        self.error = None
        self.failures = 0
        self.retry_at = 0
        self.outputs = []
        self.outputs_at = 0
        self.lock = threading.Lock()

    def settings(self):
        return {"output": self.output, "volume": self.volume, "mode": self.mode}

    def list_outputs(self, max_age=10):
        if time.time() - self.outputs_at > max_age and MONITOR_EXE.exists():
            try:
                out = subprocess.run([str(MONITOR_EXE), "--list"], capture_output=True, text=True, timeout=5,
                                     creationflags=NO_WINDOW).stdout
                found = json.loads(out)
                if isinstance(found, list):
                    self.outputs = found
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                log(f"couldn't list outputs: {e}")
            self.outputs_at = time.time()
        return self.outputs

    def target(self, capture_guid):
        """What the process should run with right now: (capture id, output id, exclusive)."""
        outputs = self.list_outputs()
        out = next((o for o in outputs if o["id"] == self.output), None)
        if not out:
            out = next((o for o in outputs if o.get("default")), None)  # "default", or a saved output that's unplugged
        out_id = out["id"] if out else "default"
        if self.mode == "auto":  # exclusive only where it can't silence other apps: outputs Windows doesn't default to
            exclusive = bool(out) and not out.get("default") and not out.get("comms")
        else:
            exclusive = self.mode == "exclusive"
        return ("{0.0.1.00000000}." + capture_guid, out_id, exclusive)

    def sync(self, capture_guid):
        """Start, stop or restart the process to match what's wanted. Called on changes and every second."""
        with self.lock:
            want = self.target(capture_guid) if self.on and capture_guid and MONITOR_EXE.exists() else None
            if want != self.running_for:
                self.retry_at = 0  # something changed: no waiting out an earlier failure
            alive = self.proc is not None and self.proc.poll() is None
            if alive and self.running_for == want:
                return
            if alive:
                self._stop()
            elif self.proc is not None:  # it exited on its own
                self.proc = None
                if want and self.running_for == want:
                    self.failures += 1
                    self.retry_at = time.time() + min(30, 2 ** self.failures)
                    log(f"monitor stopped: {self.error or 'exited'}; retrying in {min(30, 2 ** self.failures)} s")
            if self.on and not MONITOR_EXE.exists():
                self.error = "The monitor program is missing (monitor\\bin\\MicMonitor.exe)."
            if want and time.time() >= self.retry_at:
                self._start(want)

    def _start(self, want):
        cap, out, exclusive = want
        args = [str(MONITOR_EXE), "--capture", cap, "--render", out, "--volume", f"{self.volume:.3f}"]
        if exclusive:
            args.append("--exclusive")
        try:
            self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1, creationflags=NO_WINDOW)
        except OSError as e:
            self.error = f"Couldn't start the monitor: {e}"
            return
        self.running_for, self.status, self.error = want, {}, None
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict) or proc is not self.proc:
                continue
            if "error" in msg:
                err = str(msg["error"])
                hint = next((h for code, h in MONITOR_ERRORS.items() if code in err), None)
                self.error = hint or f"Monitor: {err}"
            elif "latencyMs" in msg:
                self.status = msg
                if msg.get("underruns", 0) == 0:
                    self.failures = 0

    def _stop(self):
        proc, self.proc, self.status = self.proc, None, {}
        if proc and proc.poll() is None:
            try:
                proc.stdin.write("q\n")
                proc.stdin.flush()
                proc.wait(2)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                proc.kill()

    def set_volume(self, v):
        self.volume = clamp_volume(v, self.volume)
        with self.lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.stdin.write(f"v {self.volume:.3f}\n")
                    self.proc.stdin.flush()
                except (OSError, ValueError):
                    pass

    def shutdown(self):
        with self.lock:
            self.on = False
            self._stop()

    def public(self):
        running = self.proc is not None and self.proc.poll() is None
        return {"on": self.on, "running": running, "output": self.output, "volume": self.volume, "mode": self.mode,
                "outputs": [{k: o.get(k) for k in ("id", "name", "default", "comms")} for o in self.outputs],
                "status": self.status if running else {}, "error": self.error if self.on else None}


class TrayIcon:
    """The tray icon (tray/bin/MicTray.exe). While it runs, closing the window doesn't stop the engine:
    the sound keeps running, the monitor keeps playing, and the window comes back from the tray."""

    def __init__(self):
        self.proc = None
        self.failures = 0
        self.retry_at = 0

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def keep(self):
        if self.alive() or not TRAY_EXE.exists() or time.time() < self.retry_at:
            return
        if self.proc is not None:
            self.failures += 1
            self.retry_at = time.time() + min(60, 2 ** self.failures)
            log("tray icon exited, starting it again")
            self.proc = None
            return
        try:
            # it reads stdin until it closes, so it exits with this process, however this process ends
            self.proc = subprocess.Popen([str(TRAY_EXE), "--port", str(PORT)], stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except OSError as e:
            log(f"couldn't start the tray icon: {e}")
            self.retry_at = time.time() + 60

    def stop(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            try:
                proc.stdin.close()
                proc.wait(2)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                proc.kill()


def autostart_command():
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else sys.executable}" "{CODE_FILES[0]}" --no-window'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return bool(winreg.QueryValueEx(k, RUN_NAME)[0])
    except OSError:
        return False


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


def audio_crashes(since):
    """(time, faulting module) for every crash of the Windows audio service (audiodg.exe) logged after
    `since` (epoch seconds), newest first. Reads the Application log's "Application Error" events."""
    minutes = max(1, int((time.time() - since) / 60) + 1)
    query = f"*[System[(EventID=1000) and TimeCreated[timediff(@SystemTime) <= {minutes * 60000}]]]"
    try:
        out = subprocess.run(["wevtutil", "qe", "Application", f"/q:{query}", "/f:text", "/rd:true", "/c:20"],
                             capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for event in out.split("Event[")[1:]:
        if "audiodg.exe" not in event.lower():
            continue
        module = re.search(r"Faulting module name:\s*([^,\r\n]+)", event)
        when = re.search(r"Date:\s*(\S+)", event)
        try:
            t = time.mktime(time.strptime(when.group(1)[:19], "%Y-%m-%dT%H:%M:%S")) if when else time.time()
        except ValueError:
            t = time.time()
        if module and t > since:
            found.append((t, module.group(1).strip()))
    return found


def show_existing_window():
    """Bring back the Mic Control window (also one hidden in the tray). False if there is none."""
    user32 = ctypes.windll.user32
    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def check(h, _):
        title, cls = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(ctypes.c_void_p(h), title, 256)
        user32.GetClassNameW(ctypes.c_void_p(h), cls, 256)
        if title.value == "Mic Control" and cls.value.startswith("Chrome_WidgetWin"):
            found.append(h)
            return False
        return True

    user32.EnumWindows(proc(check), None)
    if not found:
        return False
    h = ctypes.c_void_p(found[0])
    user32.ShowWindow(h, 5)   # SW_SHOW
    user32.ShowWindow(h, 9)   # SW_RESTORE
    user32.SetForegroundWindow(h)
    return True


class WatchedLock:
    """A lock that knows who holds it and since when, so a hang can be detected and located."""

    def __init__(self):
        self._lock = threading.Lock()
        self.owner = None
        self.since = 0.0

    def acquire(self, blocking=True, timeout=-1):
        ok = self._lock.acquire(blocking, timeout)
        if ok:
            self.owner, self.since = threading.current_thread(), time.monotonic()
        return ok

    def release(self):
        self.owner = None
        self._lock.release()

    __enter__ = acquire

    def __exit__(self, *exc):
        self.release()

    def locked(self):
        return self._lock.locked()

    def held_for(self):
        return time.monotonic() - self.since if self._lock.locked() else 0.0

    def holder_stack(self):
        owner = self.owner
        frame = sys._current_frames().get(owner.ident) if owner else None
        return "".join(traceback.format_stack(frame)) if frame else "(unknown)"


class MicControl:
    def __init__(self):
        self.lock = WatchedLock()
        self.lib = chain.Library()
        data = {}
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("not a settings object")
            except (OSError, ValueError) as e:
                # keep the damaged file instead of overwriting the user's presets on the next save
                broken = STATE_FILE.with_name(f"settings.broken-{time.strftime('%Y%m%d-%H%M%S')}.json")
                try:
                    shutil.copyfile(STATE_FILE, broken)
                except OSError:
                    pass
                log(f"settings.json unreadable ({e}); kept a copy as {broken.name}, starting fresh")
                data = {}
        self.applied = chain.sanitize(data.get("applied"), self.lib)
        self.current = copy.deepcopy(self.applied)
        self.builtin = chain.builtin_presets(self.lib)
        self.builtin_guitar = chain.builtin_guitar_presets(self.lib)
        self.presets = {str(k)[:40]: chain.sanitize(v, self.lib) for k, v in (data.get("presets") or {}).items()
                        if str(k)[:40] not in self.builtin}
        self.guitar_presets = {}
        for k, v in (data.get("guitarPresets") or {}).items():
            if str(k)[:40] not in self.builtin_guitar:
                self.guitar_presets[str(k)[:40]] = chain.sanitize({"route": "guitar", "guitar": v}, self.lib)["guitar"]
        self.device_id = data.get("device")
        # name/adapter of the chosen mic, kept so a brief USB dropout doesn't turn the config into "no device"
        info = data.get("deviceInfo")
        self.device_info = info if isinstance(info, dict) and info.get("id") == self.device_id else None
        self.monitor = Monitor(data.get("monitor"))
        # crash guard: plugins that crashed the audio service, switched off until the user turns them back on
        guard = data.get("crashGuard") if isinstance(data.get("crashGuard"), dict) else {}
        self.crashed = {pid: str(v)[:40] for pid, v in guard.items() if self.lib.has(pid)}
        self.lib.disabled = set(self.crashed)
        self.crash_checked = time.time()
        self.bypass = False
        self.error = None
        self.last_text = None   # what mic.txt holds now; identical writes are skipped
        self.last_write = -RELOAD_GAP
        self.flush_timer = None
        self.install_dir, self.config_dir = apo_paths()
        self.devices = []
        self.last_ping = None
        self.closing_at = None
        self.restart_requested = False
        self.quit_requested = False
        self.engine_version = "|".join(code_version(p) or "-" for p in (CODE_FILES[0], CODE_FILES[2]))
        self.refresh_devices()

    # ----- devices -------------------------------------------------------
    def refresh_devices(self):
        self.devices = capture_devices()
        if not self.device_id and self.devices:
            self.device_id = self.devices[0]["id"]  # first run: pick the Focusrite (sorted first)

    def device(self):
        return next((d for d in self.devices if d["id"] == self.device_id), None)

    def config_device(self):
        """Device to write into mic.txt: the live one, or the last known one while it's briefly missing."""
        dev = self.device()
        if dev:
            self.device_info = {k: dev[k] for k in ("id", "name", "adapter", "rate")}
            return dev
        return self.device_info

    # ----- Equalizer APO config ------------------------------------------
    @property
    def mic_file(self):
        return self.config_dir / MIC_SUBDIR / "mic.txt" if self.config_dir else None

    @property
    def plugin_dir(self):
        return self.config_dir / MIC_SUBDIR / "plugins" if self.config_dir else None

    def plugins_ok(self):
        d = self.plugin_dir
        return bool(d) and all((d / f).exists() for f in self.lib.files)  # every installed plugin got copied

    def install_plugins(self):
        """Copy the bundled plugins next to mic.txt (the audio service can't read the user profile)."""
        target = self.plugin_dir
        if not target:
            return
        target.mkdir(parents=True, exist_ok=True)
        for src in (APP_DIR / "plugins").glob("*.dll"):
            dst = target / src.name
            # Compare contents, not timestamps: the audio service keeps plugins it uses locked, so an
            # identical file must never be copied again.
            if dst.exists() and dst.stat().st_size == src.stat().st_size and file_hash(dst) == file_hash(src):
                continue
            try:
                shutil.copyfile(src, dst)
                log(f"installed {dst.name}")
            except OSError as e:  # updated plugin still in use: the old copy keeps working until the next restart
                log(f"couldn't update {dst.name} yet: {e}")

    def is_hooked(self):
        if not self.config_dir:
            return False
        try:
            text = (self.config_dir / "config.txt").read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return False
        return INCLUDE_LINE.lower() in text.lower()

    def hook(self):
        """Insert the managed blocks into config.txt, keeping everything else the user has."""
        if not self.config_dir:
            return
        cfg = self.config_dir / "config.txt"
        text = cfg.read_text(encoding="utf-8-sig", errors="replace") if cfg.exists() else ""
        backup = self.config_dir / "config.txt.before-mic-control"
        if not backup.exists() and cfg.exists():
            backup.write_text(text, encoding="utf-8")
        kept, skipping = [], False
        for line in text.splitlines():
            if line.startswith(MARK_BEGIN):
                skipping = True
            elif line.startswith(MARK_END):
                skipping = False
            elif not skipping:
                kept.append(line)
        body = "\n".join(kept).strip("\n")
        head = [f"{MARK_BEGIN}: keep the lines below off the mic >>>", "Stage: post-mix", f"{MARK_END} <<<"]
        tail = [f"{MARK_BEGIN}: mic chain >>>", "Device: all", "Stage: capture", INCLUDE_LINE, f"{MARK_END} <<<"]
        out = "\n".join(head) + "\n" + (body + "\n" if body else "") + "\n".join(tail) + "\n"
        write_atomic(cfg, out)
        log("hooked config.txt")

    def write_mic(self, now=False):
        """Write mic.txt. Every write makes Equalizer APO restart every plugin (a tiny audible gap, and
        each restart is a chance for a plugin to misbehave inside the audio service), so identical
        writes are skipped and changes closer together than RELOAD_GAP are merged into one trailing
        write. `now` skips the wait: mutes, closing, repairs."""
        if not self.mic_file:
            self.error = "Equalizer APO is not installed."
            return
        try:
            text = chain.render(self.current, self.lib, device_pattern(self.config_device()), self.plugin_dir, self.bypass)
            while self.lib.failures:
                log("plugin state failed, using its defaults: " + self.lib.failures.pop())
            if text == self.last_text and self.mic_file.exists():
                self.error = None
                return
            wait = self.last_write + RELOAD_GAP - time.monotonic()
            if wait > 0 and not now:
                if not self.flush_timer:
                    self.flush_timer = threading.Timer(wait, self._flush)
                    self.flush_timer.daemon = True
                    self.flush_timer.start()
                return
            self.mic_file.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(self.mic_file, text)
            self.last_text, self.last_write = text, time.monotonic()
            self.error = None
        except Exception as e:  # noqa: BLE001 - surface any failure in the app instead of dying
            self.error = f"Couldn't write the mic config: {e}"
            log(self.error + "\n" + traceback.format_exc())

    def check_crashes(self):
        """Crash guard: if a bundled plugin crashed the Windows audio service, switch it off so the sound
        can't keep breaking; the rest of the chain keeps working. Called every CRASH_CHECK_EVERY seconds."""
        since, self.crash_checked = self.crash_checked, time.time()
        for when, module in audio_crashes(since - 5):
            pid = self.lib.pid_for_file(module)
            log(f"Windows audio crashed in {module} at {time.strftime('%H:%M:%S', time.localtime(when))}"
                + (f"; switching {pid} off" if pid and pid not in self.crashed else ""))
            if pid and pid not in self.crashed:
                self.crashed[pid] = time.strftime("%Y-%m-%d %H:%M", time.localtime(when))
                self.lib.disabled = set(self.crashed)
                self.last_text = None  # rewrite without it
                self.write_mic(now=True)
                self.save()

    def _flush(self):
        with self.lock:
            self.flush_timer = None
            self.write_mic(now=True)  # renders whatever is current by now: the latest change wins

    def heal(self):
        """Fix what other programs break: Peace or the Equalizer APO editor rewriting config.txt,
        a plugin file deleted, mic.txt removed. Runs every few seconds; cheap when all is fine."""
        if not self.config_dir:
            return
        try:
            if not self.is_hooked():
                log("config.txt lost the mic chain (another program rewrote it), reconnecting")
                self.hook()
            if not self.plugins_ok():
                log("plugins missing, reinstalling")
                self.install_plugins()
                self.last_text = None
            if not self.mic_file.exists():
                self.last_text = None
            if self.last_text is None:
                self.write_mic(now=True)
        except OSError as e:
            self.error = f"Couldn't repair Equalizer APO's config: {e}"
            log(self.error)

    def save(self):
        data = {"applied": self.applied, "presets": self.presets, "guitarPresets": self.guitar_presets,
                "device": self.device_id, "deviceInfo": self.device_info, "monitor": self.monitor.settings(),
                "crashGuard": self.crashed}
        write_atomic(STATE_FILE, json.dumps(data, indent=1))

    def startup(self):
        with self.lock:
            if self.config_dir:
                try:
                    self.install_plugins()
                    self.hook()
                except OSError as e:
                    self.error = f"Couldn't update Equalizer APO's config folder: {e}"
                    log(self.error)
            self.write_mic(now=True)
            self.save()  # stores migrated settings in the current format

    def finalize(self):
        """Drop any preview that was never applied."""
        with self.lock:
            if self.current != self.applied or self.bypass:
                self.current = copy.deepcopy(self.applied)
                self.bypass = False
                self.write_mic(now=True)
                log("reverted unapplied preview on exit")

    # ----- API -------------------------------------------------------------
    def snapshot(self, refresh=False):
        if refresh:
            self.refresh_devices()
            self.monitor.list_outputs(max_age=3)
        dev = self.device()
        restart_needed = False
        if dev and dev["apo"]:
            changed = key_mtime(dev["apoKey"])
            restart_needed = changed is not None and changed > boot_time()
        presets = [{"name": n, "builtin": True, "settings": s} for n, s in self.builtin.items()]
        presets += [{"name": n, "builtin": False, "settings": s} for n, s in sorted(self.presets.items())]
        gpresets = [{"name": n, "builtin": True, "settings": g} for n, g in self.builtin_guitar.items()]
        gpresets += [{"name": n, "builtin": False, "settings": g} for n, g in sorted(self.guitar_presets.items())]
        return {
            "engine": self.engine_version,
            "page": code_version(CODE_FILES[1]),
            "current": self.current,
            "applied": self.applied,
            "dirty": self.current != self.applied,
            "bypass": self.bypass,
            "latency": chain.chain_latency_ms(self.current, self.lib),
            "guitarLatency": chain.guitar_latency_ms(self.current, self.lib),
            "monitor": self.monitor.public(),
            "autostart": autostart_enabled(),
            "crashGuard": [{"id": pid, "name": self.lib.plugins[pid]["name"], "when": when} for pid, when in self.crashed.items()],
            "tray": tray.alive(),
            "presets": presets,
            "guitarPresets": gpresets,
            "devices": [{k: d[k] for k in ("id", "name", "adapter", "apo", "channels", "rate")} for d in self.devices],
            "deviceId": self.device_id,
            "apo": {
                "installed": self.config_dir is not None,
                "onDevice": bool(dev and dev["apo"]),
                "restartNeeded": restart_needed,
                "hooked": self.is_hooked(),
                "plugins": self.plugins_ok(),
                "error": self.error,
            },
        }

    def handle(self, path, body):
        with self.lock:
            if path == "/api/preview":
                new = chain.sanitize(body.get("settings"), self.lib)
                # Mutes and the guitar switch belong to /api/mixer. A window showing an older state
                # must not undo them just because a slider moved.
                new["mute"] = self.current["mute"]
                new["guitar"]["mute"] = self.current["guitar"]["mute"]
                if "guitar" in (new["route"], self.current["route"]):
                    new["route"] = self.current["route"]
                new["guitar"]["on"] = new["route"] == "guitar"
                self.current = new
                self.bypass = False
                self.write_mic()
                return {"dirty": self.current != self.applied, "error": self.error,
                        "latency": chain.chain_latency_ms(self.current, self.lib),
                        "guitarLatency": chain.guitar_latency_ms(self.current, self.lib)}
            if path == "/api/apply":
                self.applied = copy.deepcopy(self.current)
                self.bypass = False
                self.write_mic()
                self.save()
            elif path == "/api/revert":
                self.current = copy.deepcopy(self.applied)
                self.bypass = False
                self.write_mic()
            elif path == "/api/bypass":
                self.bypass = bool(body.get("on"))
                self.write_mic()
            elif path == "/api/mixer":
                # Mute buttons and the guitar switch act like hardware switches: live and saved at once,
                # in both the previewed and the applied sound, without touching other unapplied edits.
                for s in (self.current, self.applied):
                    if "micMute" in body:
                        s["mute"] = bool(body["micMute"])
                    if "guitarMute" in body:
                        s["guitar"]["mute"] = bool(body["guitarMute"])
                    if "guitarOn" in body:
                        on = bool(body["guitarOn"])
                        s["guitar"]["on"] = on
                        s["route"] = "guitar" if on else "in1"
                self.write_mic(now=True)
                self.save()
            elif path == "/api/device":
                self.refresh_devices()
                if any(d["id"] == body.get("id") for d in self.devices):
                    self.device_id = body["id"]
                    self.write_mic()
                    self.save()
                    self.monitor.sync(self.device_id)
            elif path == "/api/monitor":
                m = self.monitor
                if "output" in body:
                    m.output = str(body["output"] or "default")[:200]
                if body.get("mode") in MONITOR_MODES:
                    m.mode = body["mode"]
                if "volume" in body:
                    m.set_volume(body["volume"])
                if "on" in body:
                    m.on = bool(body["on"])
                    m.failures, m.retry_at, m.error = 0, 0, None
                m.sync(self.device_id)
                if any(k in body for k in ("output", "mode", "volume")):
                    self.save()
                return {"monitor": m.public()}
            elif path == "/api/presets/save":
                name = str(body.get("name", "")).strip()[:40]
                guitar = body.get("kind") == "guitar"
                if not name or name in (self.builtin_guitar if guitar else self.builtin):
                    raise ValueError("Pick a different preset name.")
                store = self.guitar_presets if guitar else self.presets
                if name not in store and len(store) >= MAX_PRESETS:
                    raise ValueError("That's a lot of presets. Delete a few first.")
                settings = chain.sanitize(body.get("settings"), self.lib)
                store[name] = settings["guitar"] if guitar else settings
                self.save()
            elif path == "/api/presets/delete":
                (self.guitar_presets if body.get("kind") == "guitar" else self.presets).pop(str(body.get("name", "")), None)
                self.save()
            elif path == "/api/hook":
                self.hook()
                self.install_plugins()
                self.write_mic(now=True)
            elif path == "/api/open-setup":
                if not self.install_dir:
                    raise ValueError("Equalizer APO is not installed.")
                # Equalizer APO's Qt apps find their qt\platforms plugins relative to the working directory.
                os.startfile(str(self.install_dir / "DeviceSelector.exe"), cwd=str(self.install_dir))
            elif path == "/api/open-sound-settings":
                subprocess.Popen(["control.exe", "mmsys.cpl,,1"])  # Sound panel, Recording tab
            elif path == "/api/closing":
                self.closing_at = time.time()
                return {"ok": True}
            elif path == "/api/restart":
                self.restart_requested = True
                return {"ok": True}
            elif path == "/api/crash-guard/clear":
                pid = body.get("id")
                if pid in self.crashed:
                    del self.crashed[pid]
                    self.lib.disabled = set(self.crashed)
                    self.write_mic(now=True)
                    self.save()
                    log(f"{pid} turned back on")
            elif path == "/api/quit":
                self.quit_requested = True
                return {"ok": True}
            elif path == "/api/open-window":
                if not show_existing_window():
                    open_window()
                return {"ok": True}
            elif path == "/api/autostart":
                set_autostart(bool(body.get("on")))
                return {"autostart": autostart_enabled()}
            else:
                return None
            return self.snapshot(refresh=path in ("/api/device", "/api/hook"))


app = None
server = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, payload, ctype="application/json", cache=False):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _trusted(self):
        # Only answer our own page: blocks other websites and DNS-rebinding tricks.
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.rstrip("/") in {f"http://{h}" for h in ALLOWED_HOSTS}

    def do_GET(self):
        if not self._trusted():
            return self._send(403, {"error": "forbidden"})
        if self.path in ("/", "/index.html"):
            return self._send(200, (APP_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        for prefix, folder in STATIC.items():
            if self.path.startswith(prefix):
                name = Path(self.path[len(prefix):].split("?")[0]).name  # no folders, no ".."
                f = folder / name
                if f.is_file():
                    return self._send(200, f.read_bytes(), MIME.get(f.suffix, "application/octet-stream"), cache=True)
                return self._send(404, {"error": "not found"})
        if self.path == "/api/catalog":
            return self._send(200, app.lib.public())
        if self.path == "/api/state":
            with app.lock:
                return self._send(200, app.snapshot(refresh=True))
        if self.path == "/api/debug/threads":  # lock-free: what every engine thread is doing right now
            frames = sys._current_frames()
            dump = {t.name: "".join(traceback.format_stack(frames[t.ident])[-6:]) for t in threading.enumerate() if t.ident in frames}
            return self._send(200, {"lockHeld": app.lock.locked(), "threads": dump})
        if self.path == "/api/ping":
            app.last_ping = time.time()
            # the window reloads itself when either version changes
            m = app.monitor.public()
            return self._send(200, {"ok": True, "engine": app.engine_version, "page": code_version(CODE_FILES[1]),
                                    "muted": bool(app.current["mute"]), "tray": tray.alive(),
                                    "monitor": {k: m[k] for k in ("on", "running", "status", "error")}})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._trusted() or self.headers.get("X-Mic-Control") != "1":
            return self._send(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self.close_connection = True  # don't read megabytes of junk; drop the connection after answering
            return self._send(413, {"error": "Request too large."})
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            result = app.handle(self.path, body if isinstance(body, dict) else {})
        except (ValueError, KeyError, OSError, RecursionError, TypeError) as e:
            return self._send(400, {"error": str(e)[:200]})
        except Exception:  # noqa: BLE001
            log(traceback.format_exc())
            return self._send(500, {"error": "Internal error, see mic-control.log"})
        if result is None:
            return self._send(404, {"error": "not found"})
        self._send(200, result)


restart_after_shutdown = False
monitor_was_on = False
tray = TrayIcon()
use_tray = False


def current_engine_version():
    return "|".join(code_version(p) or "-" for p in (CODE_FILES[0], CODE_FILES[2]))


def watchdog():
    """Shut down once the window is gone (closed, or silent for a long time), and restart into the new
    code when it has been updated - right away if nothing is unapplied, otherwise when asked."""
    global restart_after_shutdown
    last_tick, ticks = time.time(), 0
    while True:
        time.sleep(1)
        now = time.time()
        ticks += 1
        if now - last_tick > 10 and app.last_ping:
            app.last_ping = now  # the PC slept: give the window time to wake up and ping again
        last_tick = now
        # with the tray icon running, a closed window just means "in the tray": keep going
        closed = app.closing_at and now - app.closing_at > 4 and (app.last_ping or 0) < app.closing_at and not tray.alive()
        silent = app.last_ping and now - app.last_ping > 180 and not tray.alive()
        if use_tray:
            tray.keep()
        app.monitor.sync(app.device_id)
        if not app.lock.acquire(timeout=5):
            continue
        try:
            if ticks % 5 == 0:
                app.heal()
            if ticks % CRASH_CHECK_EVERY == 0:
                app.check_crashes()
            updated = current_engine_version() != app.engine_version and "-" not in current_engine_version()
            restart = app.restart_requested or (updated and app.current == app.applied and not app.bypass)
        finally:
            app.lock.release()
        if restart:
            log("restarting into the updated engine")
            restart_after_shutdown = True
        if app.quit_requested:
            log("quit from the tray")
        if closed or silent or restart or app.quit_requested:
            server.shutdown()
            return


def stall_guard():
    """Its own thread, so it works even when the watchdog is the one that hangs: if something holds the
    settings for too long, the window can't load anymore. Record where (to fix it), then hand over to a
    fresh engine. The sound itself is unaffected, it runs in Windows. No clean shutdown: that would need
    the very lock that is stuck."""
    global monitor_was_on
    while True:
        time.sleep(2)
        stuck = app.lock.held_for()
        if stuck > LOCK_STUCK_AFTER:
            monitor_was_on = app.monitor.on  # keep hearing yourself across the restart
            log(f"engine stuck for {stuck:.0f} s, restarting. Where it hangs:\n{app.lock.holder_stack()}")
            spawn_replacement()
            os._exit(3)


def spawn_replacement():
    """Start a fresh copy of the engine that takes over once this process has exited."""
    args = [sys.executable, str(CODE_FILES[0]), "--no-window", "--wait-pid", str(os.getpid())]
    if "--port" in sys.argv:
        args += ["--port", str(PORT)]
    if monitor_was_on:
        args.append("--monitor-on")  # keep hearing yourself across the update
    subprocess.Popen(args, cwd=str(APP_DIR), creationflags=NO_WINDOW, close_fds=True)


def wait_for_pid(pid, timeout=15):
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if handle:
        k32.WaitForSingleObject(handle, int(timeout * 1000))
        k32.CloseHandle(handle)


def already_running():
    try:
        req = Request(URL + "api/ping", headers={"Host": f"127.0.0.1:{PORT}"})
        with urlopen(req, timeout=0.5) as r:
            return r.status == 200
    except OSError:
        return False


def open_window():
    candidates = [
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    ]
    for exe in candidates:
        path = os.path.expandvars(exe)
        if os.path.exists(path):
            subprocess.Popen([path, f"--app={URL}", "--window-size=1320,900", "--no-first-run",
                              "--no-default-browser-check"])
            return
    webbrowser.open(URL)


def main():
    global app, server, PORT, URL, ALLOWED_HOSTS, monitor_was_on, use_tray
    if "--port" in sys.argv:  # for running a second copy side by side, e.g. while testing
        PORT = int(sys.argv[sys.argv.index("--port") + 1])
        URL = f"http://127.0.0.1:{PORT}/"
        ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
    if "--wait-pid" in sys.argv:  # taking over from an engine that is restarting
        wait_for_pid(int(sys.argv[sys.argv.index("--wait-pid") + 1]))
    elif already_running():
        try:  # bring back the window (maybe hidden in the tray) instead of opening a second one
            urlopen(Request(URL + "api/open-window", data=b"{}", method="POST",
                            headers={"Host": f"127.0.0.1:{PORT}", "X-Mic-Control": "1"}), timeout=2).close()
        except OSError:
            open_window()
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app = MicControl()
    app.startup()
    app.monitor.on = "--monitor-on" in sys.argv
    ThreadingHTTPServer.daemon_threads = True  # a hanging request can never keep the engine from exiting
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log(f"listening on {URL} (engine {app.engine_version})")
    # test copies (--port) run without one unless asked (--tray)
    use_tray = ("--port" not in sys.argv or "--tray" in sys.argv) and "--no-tray" not in sys.argv
    if use_tray:
        tray.keep()
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=stall_guard, daemon=True).start()
    if "--no-window" not in sys.argv:
        open_window()
    try:
        server.serve_forever()
    finally:
        monitor_was_on = app.monitor.on
        app.monitor.shutdown()
        tray.stop()
        app.finalize()
        server.server_close()
        log("stopped")
        if restart_after_shutdown:
            spawn_replacement()


if __name__ == "__main__":
    main()
