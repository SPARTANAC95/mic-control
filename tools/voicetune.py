"""Analyse a voice recording like a mix engineer (a tool for hand-tuning presets, not part of the app).

    python tools/voicetune.py "Recording (40).m4a" [more files] [--json]

Reports per file: length, loudness (LUFS, the podcast standard: -16 for podcasts), true peak, noise floor,
voice pitch, and the tonal balance of the speech in the bands that make a voice deep / boxy / clear / harsh,
compared with a broadcast-podcast target.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy import signal

RATE = 48000
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

# Speech band levels as power density (dB/Hz) relative to the 1-2 kHz band. NATURAL is the ANSI S3.5-1997
# standard speech spectrum (normal effort), with the male chest band added; TARGET is a deep, clear broadcast
# voice from a close dynamic mic: more chest, cleaner box, forward presence, de-essed, a little air.
BANDS = [("chest 80-160", 80, 160), ("body 160-300", 160, 300), ("box 300-700", 300, 700),
         ("mid 1-2k", 1000, 2000), ("presence 2.5-5k", 2500, 5000), ("ess 5-9k", 5000, 9000), ("air 10-16k", 10000, 16000)]
NATURAL = {"chest 80-160": +10.0, "body 160-300": +12.0, "box 300-700": +12.0, "mid 1-2k": 0.0,
           "presence 2.5-5k": -11.0, "ess 5-9k": -19.0, "air 10-16k": -28.0}
TILT = {"chest 80-160": +4.0, "body 160-300": +1.0, "box 300-700": -3.0, "mid 1-2k": 0.0,
        "presence 2.5-5k": +3.0, "ess 5-9k": -1.0, "air 10-16k": +2.0}
TARGET = {k: NATURAL[k] + TILT[k] for k in NATURAL}


def load(path):
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).astype(np.float64)


def loudness(path):
    out = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    summary = out[out.rfind("Summary:"):]
    i = re.search(r"I:\s+(-?[\d.]+) LUFS", summary)
    lra = re.search(r"LRA:\s+([\d.]+) LU", summary)
    tp = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", summary)
    return (float(i.group(1)) if i else None, float(lra.group(1)) if lra else None,
            float(tp.group(1)) if tp and tp.group(1) != "-inf" else None)


def frames(x, n=2400):  # 50 ms
    m = len(x) // n
    return x[:m * n].reshape(m, n)


def analyse(path):
    x = load(path)
    lufs, lra, tpk = loudness(path)
    f = frames(x)
    rms = 20 * np.log10(np.sqrt((f ** 2).mean(axis=1)) + 1e-12)
    floor = float(np.percentile(rms, 8))
    speech_thr = max(floor + 12, np.percentile(rms, 60) - 10)
    speech = rms > speech_thr
    sp = f[speech].ravel() if speech.any() else x
    # tonal balance of the speech only
    fr, psd = signal.welch(sp, RATE, nperseg=8192)
    def band(lo, hi):
        sel = (fr >= lo) & (fr < hi)
        return 10 * np.log10(psd[sel].mean() + 1e-20)
    ref = band(1000, 2000)
    bal = {name: round(band(lo, hi) - ref, 1) for name, lo, hi in BANDS}
    # pitch (autocorrelation over voiced frames)
    pitches = []
    for fr_ in f[speech][::3]:
        fr_ = fr_ - fr_.mean()
        ac = np.correlate(fr_, fr_, "full")[len(fr_) - 1:]
        lo, hi = RATE // 300, RATE // 70
        k = lo + int(np.argmax(ac[lo:hi]))
        if ac[k] > 0.4 * ac[0]:
            pitches.append(RATE / k)
    # where does the boxiness peak? find the strongest bump 250-900 Hz above a smooth trend
    sel = (fr > 200) & (fr < 1200)
    db = 10 * np.log10(psd[sel] + 1e-20)
    trend = np.polyval(np.polyfit(np.log(fr[sel]), db, 2), np.log(fr[sel]))
    box_hz = float(fr[sel][np.argmax(db - trend)])
    ess_sel = (fr > 4500) & (fr < 9500)
    ess_hz = float(fr[ess_sel][np.argmax(psd[ess_sel])])
    clip = float((np.abs(x) > 0.99).mean() * 100)
    return {"file": Path(path).name, "seconds": round(len(x) / RATE, 1), "lufs": lufs, "lra": lra, "truePeak": tpk,
            "noiseFloorDb": round(floor, 1), "speechPct": round(float(speech.mean() * 100)),
            "speechLevelDb": round(float(np.median(rms[speech])) if speech.any() else 0, 1),
            "pitchHz": round(float(np.median(pitches)), 0) if pitches else None,
            "balance": bal, "vsTarget": {k: round(bal[k] - TARGET[k], 1) for k in bal},
            "boxPeakHz": round(box_hz), "essPeakHz": round(ess_hz), "clippedPct": round(clip, 3)}


def main():
    files = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = [analyse(p) for p in files]
    if "--json" in sys.argv:
        print(json.dumps(out, indent=1))
        return
    for r in out:
        print(f"\n{r['file']}: {r['seconds']} s, {r['lufs']} LUFS (LRA {r['lra']}), true peak {r['truePeak']} dBFS, "
              f"noise floor {r['noiseFloorDb']} dB, speech {r['speechPct']}% at {r['speechLevelDb']} dB, pitch {r['pitchHz']} Hz, "
              f"clipped {r['clippedPct']}%")
        print("   band              level  vs target   (+ = more than a broadcast podcast voice)")
        for name, *_ in BANDS:
            print(f"   {name:17} {r['balance'][name]:+6.1f}   {r['vsTarget'][name]:+6.1f}")
        print(f"   boxiness peaks near {r['boxPeakHz']} Hz, esses near {r['essPeakHz']} Hz")


if __name__ == "__main__":
    main()
