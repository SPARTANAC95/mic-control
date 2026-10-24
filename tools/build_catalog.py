"""Build plugins/catalog.json: every bundled plugin's controls with real values.

Loads each plugin once in the app's own VST2 host, samples every parameter at 101 normalized
positions and records what the plugin displays there ("-12.0 dB", "Hall", ...). The app uses this
to show real units on every control and to convert e.g. "-18 dB" into the plugin's 0..1 value.

Run after adding or updating a plugin:  python tools/build_catalog.py
"""
import json
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
from vsthost import Plugin  # noqa: E402

STEPS = 101

# id: file, display name, category, route ("chunk" = state blob, "names" = parameter names),
#     main = shown by default, hide = never shown (GUI-only or meaningless without a host GUI),
#     fixed = values always sent regardless of user edits. Everything else goes under Fine-tune.
CURATED = {
    # v1.21 mono build (v1.10 crashed the audio service on a config reload). A new file name, because
    # the audio service keeps a loaded plugin locked and would never pick up an update under the old one.
    "rnnoise": dict(file="rnnoise-mono.dll", name="RNNoise", vendor="werman", category="Noise", rack=False,
                    main=["VAD Threshold", "VAD Grace Period (10ms per unit)"], hide=[]),
    "kotelnikov": dict(file="TDR Kotelnikov.dll", name="TDR Kotelnikov", category="Dynamics",
                       main=["Threshold", "Ratio", "Attack", "Release Peak", "Release RMS", "Makeup", "Soft Knee"],
                       hide=["GR Disp Scale", "GR Disp Mode", "Delta", "Bypass", "Mode"], fixed={"Quality": None}),
    "nova": dict(file="TDR Nova.dll", name="TDR Nova", category="EQ",
                 main=[f"Band {b} {p}" for b in (1, 2, 3, 4) for p in ("Gain", "Frequency")] + ["Output Gain"],
                 hide=[f"Band {b} selected" for b in (1, 2, 3, 4)] + ["HP Selected", "LP Selected", "Master bypass", "Dynamic delta",
                       "Analyzer Mode", "Display EQ Range", "Display FFT Floor", "Sidechain Mode", "Analyzer Speed", "Solo", "Channels"]),
    "slickeq": dict(file="TDR SlickEQ.dll", name="TDR VOS SlickEQ", category="EQ",
                    main=["LOW Gain", "LOW Freq", "MID Gain", "MID Freq", "HIGH Gain", "HIGH Freq", "EQ Model", "OUT Gain"],
                    hide=["Bypass", "Mode", "LOW Bypass", "MID Bypass", "HIGH Bypass"],
                    defaults={"Quality": "Live"}),  # Live = zero delay (Full adds 3.8 ms); matters for guitar
    "loudmax": dict(file="LoudMax64.dll", name="LoudMax", vendor="Thomas Mundt", category="Dynamics",
                    main=["Thresh", "Output", "ISP Detection"], hide=["Large GUI", "Fader Link"]),
    "supermassive": dict(file="ValhallaSupermassive.dll", name="Valhalla Supermassive", category="Reverb & Delay",
                         main=["Mode", "Mix", "Delay_Ms", "Feedback", "Density", "Width", "ModDepth"],
                         hide=["DelaySync", "DelayNote", "Clear", "Reserved1", "Reserved2", "Reserved3", "Reserved4"],
                         defaults={"Mix": 0.25}),  # normalized (Supermassive's Mix is 0..100 %)
    "df-hall": dict(file="DragonflyHall.dll", name="Dragonfly Hall", category="Reverb",
                    main=["Dry Level", "Late Level", "Size", "Decay", "Predelay", "High Cut"], hide=[]),
    "df-plate": dict(file="DragonflyPlate.dll", name="Dragonfly Plate", category="Reverb",
                     main=["Dry Level", "Wet Level", "Algorithm", "Decay", "Predelay", "High Cut"], hide=[]),
    "df-room": dict(file="DragonflyRoom.dll", name="Dragonfly Room", category="Reverb",
                    main=["Dry Level", "Late Level", "Size", "Decay", "Predelay", "High Cut"], hide=[]),
    "df-early": dict(file="DragonflyEarly.dll", name="Dragonfly Early Reflections", category="Reverb",
                     main=["Dry Level", "Wet Level", "Program", "Size"], hide=[]),
    "graillon": dict(file="Graillon.dll", name="Graillon 3", vendor="Auburn Sounds", category="Voice FX",
                     main=["Pitch Shift", "Formant Shift", "Correction", "Corr. Amount", "Doubler", "Chorus", "Wet Mix", "Dry Mix", "Output Gain"],
                     hide=["Deleted Param", "Placebo", "MIDI In Enabled", "Tune Keyboard"]
                          + [f"Allow {n}" for n in "C C# D D# E F F# G G# A A# B".split()]
                          + [f"Bend {n}" for n in "C C# D D# E F F# G G# A A# B".split()]),
    "tse808": dict(file="TSE808.dll", name="TSE 808", vendor="TSE Audio", category="Drive",
                   main=["Drive", "Tone", "Volume"], hide=["Stereo", "Quality", "Bypass"]),
}

NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)")


def describe(p, i):
    """Sample one parameter: numeric (value + unit per step) or a list of named choices."""
    default = p.get(i)
    shown = []
    for k in range(STEPS):
        p.set(i, k / (STEPS - 1))
        shown.append(p.display(i))
    p.set(i, default)
    label = p.label(i)
    nums = []
    for s in shown:
        m = NUM.search(s.replace(",", "."))
        if "inf" in s.lower() or s.lower() in ("off",):
            nums.append(None)
        else:
            nums.append(float(m.group()) if m else None)
    distinct = list(dict.fromkeys(shown))
    numeric = sum(v is not None for v in nums) >= STEPS * 0.6 and len(distinct) > 12
    if numeric:
        unit = label or re.sub(NUM, "", shown[STEPS // 2]).strip()
        return {"kind": "number", "default": round(default, 5), "unit": unit, "shown": shown}
    # choices: the normalized position where each one starts, and its centre for sending
    choices, start = [], 0
    for k in range(1, STEPS + 1):
        if k == STEPS or shown[k] != shown[start]:
            choices.append({"text": shown[start], "value": round((start + k - 1) / 2 / (STEPS - 1), 4)})
            start = k
    return {"kind": "choice", "default": round(default, 5), "choices": choices}


def build():
    catalog = {}
    for pid, c in CURATED.items():
        p = Plugin(str(APP / "plugins" / c["file"]))
        params = []
        for i in range(p.n):
            name = p.name(i)
            if name in c.get("hide", []):
                continue
            d = describe(p, i)
            d.update(index=i, name=name, main=name in c["main"])
            if name in c.get("defaults", {}):
                want = c["defaults"][name]
                if isinstance(want, str):  # a choice by its text
                    want = next(ch["value"] for ch in d["choices"] if ch["text"] == want)
                d["default"] = want
            params.append(d)
        params.sort(key=lambda d: (not d["main"], c["main"].index(d["name"]) if d["main"] else d["index"]))
        catalog[pid] = {
            "id": pid, "file": c["file"], "name": c["name"], "vendor": c.get("vendor") or p.text(47),
            "category": c["category"], "rack": c.get("rack", True), "route": "chunk" if p.chunks else "names",
            "latency": p.latency, "params": params,
        }
        print(f"{pid:13} {c['name']:30} {catalog[pid]['route']:6} latency {p.latency:5}  {len(params)} controls")
    out = APP / "plugins" / "catalog.json"
    out.write_text(json.dumps(catalog, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    build()
