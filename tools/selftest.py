"""Mic Control self-test: runs everything through Equalizer APO's own engine (Benchmark.exe) and the app's API.

    python tools/selftest.py

Never touches the live microphone: Benchmark runs a "post-mix" copy of the config (the mic is "capture"),
config.txt is restored afterwards, and the API test uses a throwaway copy of the app on port 47829.
"""
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
import chain as C  # noqa: E402
from server import apo_paths, write_atomic  # noqa: E402
from vsthost import Plugin  # noqa: E402

RATE = 48000
INSTALL, CFG = apo_paths()
BENCH = INSTALL / "Benchmark.exe"
WORK = Path(tempfile.mkdtemp(prefix="miccontrol-selftest-"))
PLUG = CFG / "MicControl" / "selftest-plugins"   # readable by Equalizer APO, removed afterwards
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail if detail else ''}")


def wav(path, chans):
    n, ch = len(chans[0]), len(chans)
    data = struct.pack(f"<{n * ch}f", *[chans[c][i] for i in range(n) for c in range(ch)])
    path.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 3, ch, RATE, RATE * 4 * ch, 4 * ch, 32) + b"data" + struct.pack("<I", len(data)) + data)


def read(path):
    d = path.read_bytes()
    pos = 12
    while pos < len(d):
        cid, size = d[pos:pos + 4], struct.unpack_from("<I", d, pos + 4)[0]
        if cid == b"fmt ":
            ch, bits = struct.unpack_from("<H", d, pos + 10)[0], struct.unpack_from("<H", d, pos + 22)[0]
        if cid == b"data":
            body = d[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    vals = [v / 32768 for v in struct.unpack(f"<{len(body) // 2}h", body)] if bits == 16 else list(struct.unpack(f"<{len(body) // 4}f", body))
    return [vals[c::ch] for c in range(ch)]


def tone_db(x, f):
    x = x[len(x) // 2:]
    w = 2 * math.pi * f / RATE
    re_ = sum(v * math.cos(w * i) for i, v in enumerate(x))
    im = sum(v * math.sin(w * i) for i, v in enumerate(x))
    return 20 * math.log10(2 * math.hypot(re_, im) / len(x) + 1e-12)


def bench(text, src, extra=()):
    write_atomic(CFG / "MicControl" / "selftest.txt", text.replace("Stage: capture", "Stage: post-mix"))
    out = WORK / "out.wav"
    r = subprocess.run([str(BENCH), "-i", str(src), "-o", str(out), "--nopause", "-v", *extra], capture_output=True, text=True, errors="replace", timeout=300)
    log = r.stdout + r.stderr
    errors = [l.strip() for l in log.splitlines() if ("rror" in l or "crash" in l or "could not" in l.lower()) and "notification" not in l]
    cpu = next((l for l in log.splitlines() if "CPU load" in l), "")
    return read(out), errors, log.count("Adding VST plugin"), cpu


def main():
    lib = C.Library()
    print(f"Mic Control self-test  ({len(lib.plugins)} plugins in the catalog)\n")

    print("1. Plugin control conversion")
    for pid, name, value in (("kotelnikov", "Threshold", -24), ("kotelnikov", "Ratio", 3), ("kotelnikov", "Makeup", 7),
                             ("loudmax", "Output", -1), ("nova", "Band 4 Frequency", 6500), ("supermassive", "Delay_Ms", 450),
                             ("df-plate", "Decay", 1.6), ("rnnoise", "VAD Threshold", 0.5)):
        p = Plugin(str(APP / "plugins" / lib.plugins[pid]["file"]))
        i = lib.plugins[pid]["byname"][name]["index"]
        p.set(i, lib.norm(pid, name, value))
        shown = p.display(i)
        got = C._num(shown)
        check(f"{pid} {name} = {value}", got is not None and abs(got - value) <= max(0.05 * abs(value), 0.15), f"plugin shows {shown!r}")

    PLUG.mkdir(parents=True, exist_ok=True)
    for f in (APP / "plugins").glob("*.dll"):
        shutil.copyfile(f, PLUG / f.name)
    n = RATE * 2
    src = WORK / "two_tones.wav"
    wav(src, [[0.2 * math.sin(2 * math.pi * 440 * i / RATE) for i in range(n)],     # voice on Input 1
              [0.2 * math.sin(2 * math.pi * 196 * i / RATE) for i in range(n)]])    # guitar on Input 2
    config = CFG / "config.txt"
    saved = config.read_text(encoding="utf-8")
    try:
        write_atomic(config, "Include: MicControl\\selftest.txt\n")

        print("\n2. Routing, mutes and A/B (440 Hz = voice on Input 1, 196 Hz = guitar on Input 2)")
        base = C.sanitize({"gain": 0, "lowcut": {"on": False}, "limiter": {"on": False},
                           "guitar": {"gate": {"on": False}, "comp": {"on": False}, "tonecut": 16000}}, lib)
        cases = [  # route, mic mute, guitar mute, A/B bypass -> voice heard?, guitar heard?
            ("in1", False, False, False, True, False), ("in1", True, False, False, False, False),
            ("in2", False, False, False, False, True), ("mix", False, False, False, True, True),
            ("guitar", False, False, False, True, True), ("guitar", True, False, False, False, True),
            ("guitar", False, True, False, True, False), ("guitar", False, False, True, True, True),
            ("in1", False, False, True, True, False),
        ]
        for route, mm, gm, bp, want_v, want_g in cases:
            s = C.sanitize({**base, "route": route, "mute": mm, "guitar": {**base["guitar"], "mute": gm}}, lib)
            (L, R), errors, _, _ = bench(C.render(s, lib, "Benchmark", PLUG, bypass=bp), src)
            v, g = tone_db(L, 440), tone_db(L, 196)
            same = max(abs(a - b) for a, b in zip(L, R)) < 1e-3
            ok = (v > -30) == want_v and (g > -30) == want_g and same and not errors
            check(f"route={route:6} mute={mm!s:5} guitarMute={gm!s:5} A/B={bp!s:5}", ok, f"voice {v:6.1f} dB, guitar {g:6.1f} dB, L=R {same}{', ' + errors[0] if errors else ''}")

        print("\n3. Every rack plugin inside the real engine")
        for pid, p in lib.plugins.items():
            if not p.get("rack", True):
                continue
            s = C.sanitize({**base, "rack": [{"plugin": pid, "on": True, "values": {}}]}, lib)
            (L, _), errors, added, _ = bench(C.render(s, lib, "Benchmark", PLUG), src)
            level = tone_db(L, 440)
            check(f"{p['name']:30}", added >= 1 and not errors and level > -40, f"loaded {added}, voice {level:.1f} dB{', ' + errors[0] if errors else ''}")

        # Warmth (SlickEQ "Deep" stage): adds low-end harmonics but auto gain keeps the level
        loud = WORK / "loud110.wav"
        wav(loud, [[0.5 * math.sin(2 * math.pi * 110 * i / RATE) for i in range(n)]] * 2)
        plain = C.sanitize({**base, "route": "in1"}, lib)
        (L0, _), _, _, _ = bench(C.render(plain, lib, "Benchmark", PLUG), loud)
        (L1, _), errors, added, _ = bench(C.render(C.sanitize({**plain, "warmth": {"on": True, "drive": 18}}, lib), lib, "Benchmark", PLUG), loud)
        h3_0, h3_1, lvl0, lvl1 = tone_db(L0, 330), tone_db(L1, 330), tone_db(L0, 110), tone_db(L1, 110)
        check("warmth adds harmonics at the same level", not errors and added == 1 and h3_1 > h3_0 + 20 and abs(lvl1 - lvl0) < 1.5,
              f"3rd harmonic {h3_0:.0f} -> {h3_1:.0f} dB, level {lvl0:.1f} -> {lvl1:.1f} dB")
        # crash guard: a switched-off plugin leaves the chain, everything else stays
        lib.disabled = {"kotelnikov"}
        guarded = C.render(C.sanitize({**plain, "comp": {"on": True}, "deesser": {"on": True}}, lib), lib, "Benchmark", PLUG)
        lib.disabled = set()
        check("crash guard leaves out only the switched-off plugin", "Kotelnikov" not in guarded and "TDR Nova" in guarded)

        print("\n4. Built-in presets and the everything-on worst case")
        for name, ps in C.builtin_presets(lib).items():
            (L, _), errors, added, _ = bench(C.render(ps, lib, "Benchmark", PLUG), src)
            peak = 20 * math.log10(max(abs(x) for x in L) + 1e-12)
            check(f"mic preset {name:16}", not errors and peak <= -0.9, f"{added} plugins, peak {peak:.2f} dB")
        for name, gp in C.builtin_guitar_presets(lib).items():
            s = C.sanitize({**C.builtin_presets(lib)["Podcast Deep"], "route": "guitar", "guitar": gp}, lib)
            (L, _), errors, added, _ = bench(C.render(s, lib, "Benchmark", PLUG), src)
            check(f"guitar preset {name:13}", not errors and tone_db(L, 196) > -40, f"{added} plugins, guitar {tone_db(L, 196):.1f} dB")
        hot = WORK / "hot.wav"
        wav(hot, [[0.9 * math.sin(2 * math.pi * 440 * i / RATE) for i in range(n)], [0.9 * math.sin(2 * math.pi * 196 * i / RATE) for i in range(n)]])
        worst = C.sanitize({**C.builtin_presets(lib)["Podcast Deep"], "route": "guitar", "gain": 12,
                            "rack": [{"plugin": k, "on": True, "values": {}} for k in ("df-hall", "supermassive", "graillon", "slickeq")],
                            "guitar": C.builtin_guitar_presets(lib)["Crunch"]}, lib)
        (L, R), errors, added, cpu = bench(C.render(worst, lib, "Benchmark", PLUG), hot)
        peak = 20 * math.log10(max(abs(x) for x in L) + 1e-12)
        check("everything on, +12 dB boost, hot input", not errors and peak <= -0.9, f"{added} plugins, peak {peak:.2f} dB (ceiling -1.0), {cpu.strip()}")
    finally:
        write_atomic(config, saved.replace("\r\n", "\n"))
        (CFG / "MicControl" / "selftest.txt").unlink(missing_ok=True)
        shutil.rmtree(PLUG, ignore_errors=True)
    check("config.txt restored", config.read_text(encoding="utf-8").replace("\r\n", "\n") == saved.replace("\r\n", "\n"))

    print("\n5. App API on a throwaway copy")
    api_test(lib)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
    shutil.rmtree(WORK, ignore_errors=True)
    return 1 if failed else 0


def api_test(lib):
    port = 47829
    home, apo = WORK / "home", WORK / "apo"
    home.mkdir(); apo.mkdir()
    old = {"applied": {"gain": 6, "comp": {"on": True, "thresh": -24, "ratio": 3, "makeup": 4},
                       "reverb": {"on": True, "mix": 20, "decay": 1.2}, "delay": {"on": True, "time": 320, "mix": 18, "feedback": 30}},
           "presets": {"Old one": {"gain": 3}}}
    (home / "settings.json").write_text(json.dumps(old), encoding="utf-8")
    env = dict(os.environ, MICCONTROL_HOME=str(home), MICCONTROL_APO_CONFIG=str(apo))
    proc = subprocess.Popen([sys.executable, str(APP / "server.py"), "--no-window", "--port", str(port)], env=env, cwd=str(APP))

    def call(path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Mic-Control": "1", "Content-Type": "application/json"}, method="GET" if body is None else "POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    try:
        for _ in range(50):
            try:
                s = call("/api/state")
                break
            except OSError:
                time.sleep(0.2)
        cur = s["current"]
        check("old settings migrated", cur["version"] == 2 and cur["gain"] == 6 and [x["plugin"] for x in cur["rack"]] == ["df-room", "supermassive"],
              f"rack {[x['plugin'] for x in cur['rack']]}")
        check("old custom preset kept", any(p["name"] == "Old one" for p in s["presets"]))
        cur["bass"]["gain"] = 5
        r = call("/api/preview", {"settings": cur})
        check("preview marks unapplied", r["dirty"] is True)
        time.sleep(0.4)  # quick changes are merged into one Equalizer APO reload
        mic = (apo / "MicControl" / "mic.txt").read_text(encoding="utf-8")
        check("preview reaches mic.txt", "LSC Fc 120 Hz Gain 5.0 dB" in mic)
        stamps, mic_file = set(), apo / "MicControl" / "mic.txt"
        t0 = time.time()
        for g in range(40):
            call("/api/preview", {"settings": dict(cur, bass=dict(cur["bass"], gain=g % 9 - 4))})
            stamps.add(mic_file.stat().st_mtime_ns)
        took = time.time() - t0
        time.sleep(0.4)
        stamps.add(mic_file.stat().st_mtime_ns)
        check("40 rapid previews -> few plugin reloads, the last one wins",
              len(stamps) <= 2 + took / 0.3 and f"LSC Fc 120 Hz Gain {39 % 9 - 4:.1f} dB" in mic_file.read_text(encoding="utf-8"),
              f"{len(stamps)} writes seen in {took:.2f} s")
        call("/api/preview", {"settings": cur})
        s = call("/api/mixer", {"micMute": True})
        check("mute applies instantly and is saved", s["current"]["mute"] and s["applied"]["mute"])
        stale = dict(cur, mute=False)
        call("/api/preview", {"settings": stale})
        check("a stale window can't unmute", call("/api/state")["current"]["mute"] is True)
        call("/api/mixer", {"micMute": False})
        s = call("/api/revert", {})
        check("revert restores the applied sound", s["current"]["bass"]["gain"] == 0 and not s["dirty"])
        s = call("/api/mixer", {"guitarOn": True})
        check("guitar switch sets the route", s["current"]["route"] == "guitar" and s["current"]["guitar"]["on"])
        s = call("/api/presets/save", {"name": "Test sound", "kind": "mic", "settings": s["current"]})
        check("save mic preset", any(p["name"] == "Test sound" and not p["builtin"] for p in s["presets"]))
        s = call("/api/presets/save", {"name": "Test guitar", "kind": "guitar", "settings": s["current"]})
        check("save guitar preset", any(p["name"] == "Test guitar" for p in s["guitarPresets"]))
        try:
            call("/api/presets/save", {"name": "Flat", "kind": "mic", "settings": s["current"]})
            check("built-in preset names protected", False)
        except urllib.error.HTTPError as e:
            check("built-in preset names protected", e.code == 400)
        s = call("/api/bypass", {"on": True})
        time.sleep(0.4)
        mic = (apo / "MicControl" / "mic.txt").read_text(encoding="utf-8")
        check("A/B removes the effects", s["bypass"] and "VSTPlugin" not in mic)
        call("/api/bypass", {"on": False})
        bad = dict(s["current"], gain=999, rack=[{"plugin": "nope"}, {"plugin": "df-plate", "values": {"Decay": 7, "Bogus": 1}}])
        s2 = call("/api/preview", {"settings": bad})
        st = call("/api/state")
        check("bad input is clamped and filtered", st["current"]["gain"] == 24 and [x["plugin"] for x in st["current"]["rack"]] == ["df-plate"]
              and st["current"]["rack"][0]["values"] == {"Decay": 1})
        try:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/apply", data=b"{}", headers={"Host": "evil.example"}, method="POST"), timeout=5)
            check("foreign sites are refused", False)
        except urllib.error.HTTPError as e:
            check("foreign sites are refused", e.code == 403)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
