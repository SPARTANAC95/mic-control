"""Mic Control stress test, on a throwaway copy of the app (never the live mic).

    python tools/stresstest.py

1. Fuzz: 1500 random and garbage settings -> only 200/400 answers, no crash, response times.
2. Every 75th random config runs through Equalizer APO itself (Benchmark): no errors, L=R, limiter ceiling holds.
3. Concurrency: 12 clients hammering previews, mutes, presets and state at once -> consistent end state.
4. Bad requests: broken JSON, huge bodies, unknown paths, foreign sites.
5. Self-repair: config.txt rewritten by "another program", plugin deleted, mic.txt deleted, settings.json corrupted.
6. Memory: engine memory before/after all of the above.
7. Monitor (at volume 0): junk values, racing on/off clicks, the engine crashing under it.
"""
import concurrent.futures as cf
import ctypes
import json
import math
import os
import random
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
import chain as C  # noqa: E402
from server import apo_paths, write_atomic  # noqa: E402

PORT = 47828
INSTALL, REAL_CFG = apo_paths()
WORK = Path(tempfile.mkdtemp(prefix="miccontrol-stress-"))
HOME, APO = WORK / "home", WORK / "apo"
results = []
rng = random.Random(7)


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail if detail else ''}", flush=True)


def call(path, body=None, raw=None, headers=None, timeout=20):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    h = {"X-Mic-Control": "1", "Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, headers=h, method="GET" if data is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, None


def start():
    env = dict(os.environ, MICCONTROL_HOME=str(HOME), MICCONTROL_APO_CONFIG=str(APO))
    p = subprocess.Popen([sys.executable, str(APP / "server.py"), "--no-window", "--port", str(PORT)], env=env, cwd=str(APP))
    for _ in range(100):
        try:
            if call("/api/ping")[0] == 200:
                return p
        except OSError:
            time.sleep(0.15)
    raise RuntimeError("engine didn't start")


def monitor_children(pid):
    q = f"@(Get-CimInstance Win32_Process -Filter \"ParentProcessId={pid} AND Name='MicMonitor.exe'\").Count"
    out = subprocess.run(["powershell", "-NoProfile", "-Command", q], capture_output=True, text=True).stdout
    return int(out.strip() or 0)


def memory_mb(pid):
    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
            "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
    h = ctypes.windll.kernel32.OpenProcess(0x0410, False, pid)
    c = PMC(); c.cb = ctypes.sizeof(c)
    ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb)
    ctypes.windll.kernel32.CloseHandle(h)
    return c.WorkingSetSize / 2 ** 20


def random_settings(lib):
    """Mostly valid but wild settings, sometimes with junk mixed in."""
    def num(lo, hi):
        return rng.choice([rng.uniform(lo, hi), rng.uniform(lo * 3 - 50, hi * 3 + 50), "x", None, float("nan"), 1e308]) if rng.random() < 0.15 else rng.uniform(lo, hi)
    s = {"version": 2, "enabled": rng.random() > 0.1, "route": rng.choice(list(C.ROUTES) + ["bogus"]), "mute": False}
    for key, (lo, hi) in C.LIMITS.items():
        parts, target = key.split("."), s
        for p in parts[:-1]:
            target = target.setdefault(p, {})
        target[parts[-1]] = num(lo, hi)
    for key in C.TOGGLES:
        target = s
        for p in key.split("."):
            target = target.setdefault(p, {})
        target["on"] = rng.random() > 0.4
    s["denoise"]["strength"] = rng.choice(["light", "medium", "strong", "loud"])
    plugs = [k for k, v in lib.plugins.items() if v.get("rack", True)] + ["nope"]
    def rack():
        out = []
        for _ in range(rng.randint(0, 10)):
            pid = rng.choice(plugs)
            params = lib.plugins[pid]["params"] if pid in lib.plugins else []
            vals = {q["name"]: rng.random() for q in rng.sample(params, min(len(params), rng.randint(0, 6)))}
            if rng.random() < 0.1:
                vals["Junk"] = "??"
            out.append({"uid": rng.choice(["a", "b", "x" * 40, None, "dup"]), "plugin": pid, "on": rng.random() > 0.2, "values": vals})
        return out
    s["rack"] = rack()
    s["guitar"]["rack"] = rack()
    if rng.random() < 0.05:
        s = rng.choice([[], "string", 42, {"guitar": "nope", "rack": "nope"}])
    return s


def bench_check(text, lib):
    """Run a config through Equalizer APO's engine with a loud two-tone input."""
    bench = INSTALL / "Benchmark.exe"
    src = WORK / "in.wav"
    if not src.exists():
        n = 48000
        L = [0.9 * math.sin(2 * math.pi * 440 * i / 48000) for i in range(n)]
        R = [0.9 * math.sin(2 * math.pi * 196 * i / 48000) for i in range(n)]
        data = struct.pack(f"<{2 * n}f", *[v for pair in zip(L, R) for v in pair])
        src.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 3, 2, 48000, 384000, 8, 32) + b"data" + struct.pack("<I", len(data)) + data)
    write_atomic(REAL_CFG / "MicControl" / "stress.txt", text.replace("Stage: capture", "Stage: post-mix").replace("Device: ", "Device: all\n# "))
    out = WORK / "out.wav"
    r = subprocess.run([str(bench), "-i", str(src), "-o", str(out), "--nopause", "-v"], capture_output=True, text=True, errors="replace", timeout=300)
    log = r.stdout + r.stderr
    errors = [l.strip() for l in log.splitlines() if ("rror" in l or "crash" in l) and "notification" not in l]
    d = out.read_bytes()
    body = d[d.find(b"data") + 8:]
    v = struct.unpack(f"<{len(body) // 2}h", body[:len(body) // 2 * 2])
    L, R = v[0::2], v[1::2]
    peak = 20 * math.log10(max(abs(x) for x in L) / 32768 + 1e-12)
    return errors, max(abs(a - b) for a, b in zip(L, R)) <= 1, peak


def main():
    lib = C.Library()
    HOME.mkdir(); APO.mkdir()
    print("Mic Control stress test\n", flush=True)
    proc = start()
    mem0 = memory_mb(proc.pid)
    config, saved = REAL_CFG / "config.txt", (REAL_CFG / "config.txt").read_text(encoding="utf-8")
    try:
        # ------------------------------------------------------------------ 1 + 2
        print("1-2. Fuzzing 1500 settings (every 75th through Equalizer APO itself)", flush=True)
        write_atomic(config, "Include: MicControl\\stress.txt\n")
        codes, times, bench_bad = {}, [], []
        for i in range(1500):
            t = time.perf_counter()
            code, body = call("/api/preview", {"settings": random_settings(lib)})
            times.append((time.perf_counter() - t) * 1000)
            codes[code] = codes.get(code, 0) + 1
            if i % 75 == 0 and code == 200:
                time.sleep(0.35)  # let the merged write land
                text = (APO / "MicControl" / "mic.txt").read_text(encoding="utf-8")
                st = call("/api/state")[1]["current"]
                errors, same, peak = bench_check(text, lib)
                limited = st["enabled"] and st["limiter"]["on"]
                same = same or st["route"] == "stereo"  # stereo keeps left and right separate on purpose
                if errors or not same or (limited and peak > st["limiter"]["ceiling"] + 0.3):
                    bench_bad.append((i, errors[:1], same, round(peak, 2), st["limiter"]))
        check("only 200/400 answers, no crash", set(codes) <= {200, 400} and proc.poll() is None, f"{codes}")
        times.sort()
        check("preview response times", times[len(times) // 2] < 50 and times[int(len(times) * 0.95)] < 250,
              f"median {statistics.median(times):.1f} ms, 95% under {times[int(len(times) * 0.95)]:.1f} ms, worst {times[-1]:.0f} ms (includes plugin state builds)")
        check("random configs run clean in Equalizer APO (L=R, ceiling holds)", not bench_bad, f"{20 - len(bench_bad)}/20 ok {bench_bad[:2]}")

        # ------------------------------------------------------------------ 3
        print("\n3. Concurrency: 12 clients x 120 mixed requests", flush=True)
        call("/api/revert", {})
        base = call("/api/state")[1]["current"]
        errors = []
        def client(k):
            r = random.Random(k)
            for j in range(120):
                op = r.random()
                try:
                    if op < 0.45:
                        s = json.loads(json.dumps(base)); s["gain"] = r.uniform(-12, 24); s["bass"]["gain"] = r.uniform(-12, 15)
                        code, _ = call("/api/preview", {"settings": s})
                    elif op < 0.6:
                        code, _ = call("/api/mixer", r.choice([{"micMute": r.random() > .5}, {"guitarMute": r.random() > .5}, {"guitarOn": r.random() > .5}]))
                    elif op < 0.75:
                        code, _ = call("/api/state")
                    elif op < 0.85:
                        code, _ = call("/api/presets/save", {"name": f"c{k}-{j % 5}", "kind": r.choice(["mic", "guitar"]), "settings": base})
                    elif op < 0.92:
                        code, _ = call("/api/presets/delete", {"name": f"c{k}-{r.randint(0, 4)}", "kind": r.choice(["mic", "guitar"])})
                    else:
                        code, _ = call("/api/bypass", {"on": r.random() > .5})
                    if code not in (200, 400):
                        errors.append(code)
                except Exception as e:  # noqa: BLE001
                    errors.append(repr(e))
        t = time.time()
        with cf.ThreadPoolExecutor(12) as ex:
            list(ex.map(client, range(12)))
        check("1440 concurrent requests without errors", not errors and proc.poll() is None, f"{time.time() - t:.1f} s, errors {errors[:3]}")
        st = call("/api/state")[1]
        time.sleep(0.4)
        disk = json.loads((HOME / "settings.json").read_text(encoding="utf-8"))
        mic = (APO / "MicControl" / "mic.txt").read_text(encoding="utf-8")
        consistent = (disk["applied"]["mute"] == st["applied"]["mute"] and disk["applied"]["route"] == st["applied"]["route"]
                      and sorted(disk["presets"]) == sorted(p["name"] for p in st["presets"] if not p["builtin"]))
        check("saved file matches the engine after the storm", consistent)
        expected = C.render(C.sanitize(st["current"], lib), lib, "", APO / "MicControl" / "plugins", st["bypass"]).split("\n", 3)[3]
        check("mic.txt matches the engine's current sound", mic.split("\n", 3)[3] == expected)

        # ------------------------------------------------------------------ 4
        print("\n4. Bad requests", flush=True)
        check("broken JSON -> 400", call("/api/preview", raw=b"{not json")[0] == 400)
        check("deeply nested JSON -> 400", call("/api/preview", raw=b'{"settings":' + b"[" * 60000 + b"]" * 60000 + b"}")[0] == 400 and proc.poll() is None)
        try:
            big = call("/api/preview", raw=b" " * 3_000_000)[0]
        except OSError:
            big = "connection dropped"
        check("3 MB body -> refused, engine fine", big in (413, "connection dropped") and call("/api/ping")[0] == 200, str(big))
        check("unknown path -> 404", call("/api/nope", {})[0] == 404)
        check("missing app header -> 403", call("/api/apply", {}, headers={"X-Mic-Control": "0"})[0] == 403)
        check("foreign Origin -> 403", call("/api/apply", {}, headers={"Origin": "https://evil.example"})[0] == 403)
        check("path tricks on fonts -> 404", call("/fonts/..%5Cserver.py")[0] == 404)

        # ------------------------------------------------------------------ 5
        print("\n5. Self-repair", flush=True)
        (APO / "config.txt").write_text("Include: peace.txt\n", encoding="utf-8")
        victim = APO / "MicControl" / "plugins" / "DragonflyRoom.dll"
        victim.unlink()
        (APO / "MicControl" / "mic.txt").unlink()
        time.sleep(7)
        healed = "MicControl\\mic.txt" in (APO / "config.txt").read_text(encoding="utf-8")
        check("config.txt rewritten by another program -> reconnected", healed)
        check("deleted plugin -> reinstalled", victim.exists())
        check("deleted mic.txt -> rewritten", (APO / "MicControl" / "mic.txt").exists())
        mem1 = memory_mb(proc.pid)
        check("memory stays bounded", mem1 - mem0 < 150, f"{mem0:.0f} MB -> {mem1:.0f} MB after ~3000 requests")
        proc.terminate(); proc.wait(10)
        (HOME / "settings.json").write_text("{ this is not json", encoding="utf-8")
        proc = start()
        broken = list(HOME.glob("settings.broken-*.json"))
        check("corrupted settings.json -> engine starts, damaged file kept", proc.poll() is None and len(broken) == 1)

        # ------------------------------------------------------------------ 7
        print("\n7. Monitor (volume 0, nothing audible)", flush=True)
        call("/api/monitor", {"volume": 0})
        codes = [call("/api/monitor", junk)[0] for junk in (
            {"volume": "loud"}, {"volume": None}, {"volume": [1]}, {"volume": True}, {"mode": 5}, {"mode": "loud"},
            {"output": ["x"]}, {"output": None}, {"on": "yes"}, {"on": False})]
        m = call("/api/state")[1]["monitor"]
        check("junk monitor values ignored, volume stays 0", set(codes) == {200} and m["volume"] == 0 and m["mode"] == "auto", f"{codes}")
        def flip(k):
            for j in range(10):
                call("/api/monitor", {"on": (k + j) % 2 == 0})
        with cf.ThreadPoolExecutor(4) as ex:
            list(ex.map(flip, range(4)))
        call("/api/monitor", {"on": True})
        time.sleep(3)
        mon = call("/api/ping")[1]["monitor"]
        n = monitor_children(proc.pid)
        check("40 racing on/off clicks -> exactly one monitor, playing", mon["running"] and n == 1 and mon["status"].get("latencyMs"),
              f"{n} process(es), {mon['status'].get('latencyMs')} ms, {mon['status'].get('mode')}")
        call("/api/monitor", {"on": False})
        time.sleep(0.5)
        check("off -> monitor process gone", monitor_children(proc.pid) == 0)
        call("/api/monitor", {"on": True})
        time.sleep(1.5)
        pid = proc.pid
        proc.kill()
        proc.wait(5)
        time.sleep(2)
        check("engine killed -> monitor quits by itself", monitor_children(pid) == 0)
    finally:
        write_atomic(config, saved.replace("\r\n", "\n"))
        (REAL_CFG / "MicControl" / "stress.txt").unlink(missing_ok=True)
        proc.terminate()
        proc.wait(10)
    check("real config.txt restored", config.read_text(encoding="utf-8").replace("\r\n", "\n") == saved.replace("\r\n", "\n"))
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
    shutil.rmtree(WORK, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
