# Mic Control

Pro-level processing for your microphone (and a guitar on Input 2) that works in **every app**:
Discord, OBS, games, browsers. It runs inside Windows' own audio engine through
[Equalizer APO](https://sourceforge.net/projects/equalizerapo/), so apps get the finished sound,
even while this window is closed.

- Noise gate, AI noise suppression (RNNoise), EQ with named bands, warmth (analog-style saturation),
  de-esser, compressor and a true-peak limiter
- Effects rack: reverbs (Valhalla Supermassive, Dragonfly), voice FX (Graillon), drive (TSE 808)
- Guitar on Input 2 with its own chain, mute and presets
- Presets, live preview with Apply / Revert, A/B against the untouched mic
- Low-latency "hear yourself" monitor (native WASAPI, ~17-30 ms)
- Tray icon: minimize or close the window and it keeps running; mute and monitor from the tray
- Crash guard: a plugin that crashes Windows audio gets switched off automatically

## Install

1. **Equalizer APO**: install it from <https://sourceforge.net/projects/equalizerapo/>. In its
   *Device Selector*, tick your microphone under **Capture devices**, click OK, then **restart the PC**.
2. **Download this repo** (green *Code* button, *Download ZIP*) and unzip it somewhere permanent,
   for example `Documents\MicControl`.
3. Double-click **`setup.bat`**. It installs Python and the .NET 9 Desktop Runtime if they're missing,
   downloads the plugins from their makers' websites, and puts a **Mic Control** shortcut on your desktop.
4. Open **Mic Control**. Pick your mic in *Settings*, then choose a preset on the *Mic* page.

Using a Focusrite (or another 2-input interface)? In Windows sound settings, set the input to
**2 channels, 48000 Hz**, so the guitar on Input 2 gets its own channel.

## Tips

- Leave the Windows recording level at 100% and turn off "automatically adjust microphone" in Discord.
- For the best sound, record a raw sample of your voice and tune from there. Distance to the mic
  changes everything: a dynamic mic at 30 cm sounds thin, at 5-10 cm full and deep.
- The status bar's **Monitor** button lets you hear yourself; use headphones.

## Plugins

The plugins are free, but most may **not** be redistributed, so they aren't in this repo.
`tools/get_plugins.py` (run by `setup.bat`) downloads each one from its official source and checks it
against the exact version Mic Control was tested with. Installers are never run: they are unpacked.

| Plugin | Maker | Used for |
|---|---|---|
| RNNoise 1.21 | werman (GPL-3) | AI noise suppression |
| TDR Nova / Kotelnikov / VOS SlickEQ | Tokyo Dawn Labs | de-esser, compressor, warmth |
| LoudMax 1.47 | Thomas Mundt | limiter |
| ReaGate / ReaComp (ReaPlugs 2.36) | Cockos | noise gate, guitar sustain |
| Valhalla Supermassive | Valhalla DSP | reverb / delay |
| Dragonfly Hall / Plate / Room / Early | Michael Willis (GPL-3) | reverbs |
| Graillon 3 Free | Auburn Sounds | voice FX |
| TSE 808 | TSE Audio | guitar drive |

All plugins belong to their makers and are used under their own licenses.
Fonts: Lexend, SIL Open Font License (`fonts/OFL.txt`).

## For developers

- `server.py`: the engine (local web server on port 47820, writes Equalizer APO's config, runs the monitor and tray)
- `chain.py`: settings and the Equalizer APO config it generates
- `index.html`: the app UI
- `monitor/`, `tray/`: the native monitor and tray icon (.NET 9; `dotnet publish -c Release -o bin`)
- `tools/selftest.py`, `tools/stresstest.py`: tests (they swap Equalizer APO's config for a few seconds)
