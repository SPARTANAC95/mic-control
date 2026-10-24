"""Download Mic Control's plugins from their makers' official sites into plugins/.

    python tools/get_plugins.py            (skips plugins that are already there)
    python tools/get_plugins.py --force    (downloads everything again)

Most of these plugins are free to use but may not be redistributed, so they can't ship with Mic Control:
this script fetches each one from its official source. Installers are never run: Inno Setup installers
are unpacked with innoextract (from its GitHub releases), NSIS installers with the small reader below.
Every file is checked against the SHA-256 of the exact version Mic Control was tested with; a different
(newer) version is still installed, with a warning.
"""
import hashlib
import io
import lzma
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
import zlib
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
DEST = APP / "plugins"
INNOEXTRACT = "https://github.com/dscharrer/innoextract/releases/download/1.9/innoextract-1.9-windows.zip"
TDR = "https://www.tokyodawn.net/labs/"

# target file: (sha256 of the tested version, archive url, how to get the dll out of it)
PLUGINS = {
    "rnnoise-mono.dll": ("664ce729baca985652c24515593e43a7c0105f7a0fb64e75b6b90776b9bd6495",
                         "https://github.com/werman/noise-suppression-for-voice/releases/download/v1.21/win-rnnoise.zip",
                         ("zip", "win-rnnoise/vst/rnnoise_mono.dll")),
    "TDR Kotelnikov.dll": ("6ea04f20e46c9e214739441b67de7c219fd4fa34e91e59aa0e68d7e1f67a2438",
                           TDR + "Kotelnikov/1.6.7/TDR%20Kotelnikov%20(no%20installer).zip",
                           ("zip", "TDR Kotelnikov (no installer)/VST2/x64/TDR Kotelnikov.dll")),
    "TDR Nova.dll": ("149fff806b68f9332322d9244f552b7a1b52be99c1a62516ada4a4f18dcfe48d",
                     TDR + "Nova/2.2.3/TDR%20Nova%20(no%20installer).zip",
                     ("zip", "TDR Nova (no installer)/VST2/x64/TDR Nova.dll")),
    "TDR SlickEQ.dll": ("333ba09fcdd333c6f5065fbe5eac50c1852f8de8ca69c3100c991bd91bc1c560",
                        TDR + "SlickEQ/1.3.9/TDR%20VOS%20SlickEQ%20(no%20installer).zip",
                        ("zip", "TDR VOS SlickEQ (no installer)/VST2/x64/TDR VOS SlickEQ.dll")),
    "LoudMax64.dll": ("fbc6783043c06f36ac4b5b5a4cb1ae90e0ee19bbd91fad0ce4ad456ef51bb012",
                      # the maker's own Dropbox link, as published on loudmax.blogspot.com
                      "https://www.dropbox.com/scl/fi/32w2qx7qtr8d14bzzrnwr/LoudMax_v1_47_WIN_VST2.zip?rlkey=8nnbs9speatvm40fy9iax69kb&dl=1",
                      ("zip", "LoudMax64.dll")),
    "TSE808.dll": ("57c5ec7d11dbf1c394004a626bbe3d3984d254d880ee3924c74ca09afdd8da41",
                   "https://www.tseaudio.com/files/TSE808_win64.zip",
                   ("zip", "TSE_808_2.0_x64.dll")),
    "DragonflyEarly.dll": ("b30361386ed23a2d4f0b717afb491e27d4349490671d38a3b9afc9884182d03f",
                           "https://github.com/michaelwillis/dragonfly-reverb/releases/download/3.2.10/dragonfly-reverb-3.2.10-win64.zip",
                           ("zip", "dragonfly-reverb-3.2.10/DragonflyEarlyReflections-vst.dll")),
    "DragonflyHall.dll": ("d4b2f1ff7bffbf9bf054b00a481e740636655be8944b5c7af2d6b9f0450bfffd",
                          "https://github.com/michaelwillis/dragonfly-reverb/releases/download/3.2.10/dragonfly-reverb-3.2.10-win64.zip",
                          ("zip", "dragonfly-reverb-3.2.10/DragonflyHallReverb-vst.dll")),
    "DragonflyPlate.dll": ("b02dee977abcd3d90952c4019bf49a548c3bc51759c51b376226edc4c8ef700e",
                           "https://github.com/michaelwillis/dragonfly-reverb/releases/download/3.2.10/dragonfly-reverb-3.2.10-win64.zip",
                           ("zip", "dragonfly-reverb-3.2.10/DragonflyPlateReverb-vst.dll")),
    "DragonflyRoom.dll": ("8ec98026268fc427717ef80566577134a3213b5552a1e062496f1804259c1a7b",
                          "https://github.com/michaelwillis/dragonfly-reverb/releases/download/3.2.10/dragonfly-reverb-3.2.10-win64.zip",
                          ("zip", "dragonfly-reverb-3.2.10/DragonflyRoomReverb-vst.dll")),
    "ValhallaSupermassive.dll": ("10c97dd93a94198577654c5c4ce8725a00a57050d9baca32aa9867af6d234ac1",
                                 "https://valhallaproduction.s3.us-west-2.amazonaws.com/supermassive/ValhallaSupermassiveWin_V5_0_0.zip",
                                 ("inno", "ValhallaSupermassiveWin_V5_0_0.exe", "ValhallaSupermassive")),
    "Graillon.dll": ("1d4b2a1d35174624a3e57621dc45b103a5438cb4bc8939683b7494a7b204e04a",
                     "https://www.auburnsounds.com/downloads/Graillon-FREE-3.2.zip",
                     ("nsis", "Graillon-FREE-3.2/Windows/Graillon-3-FREE-3.2.0.exe", b"Graillon")),
    "reagate-standalone.dll": ("36cde76c3dbd1d79275c5e52a7120c3ccb1e21ae144cfd5f0e70a0f939e29998",
                               "https://www.reaper.fm/reaplugs/reaplugs236_x64-install.exe",
                               ("nsis", None, b"ReaGate")),
    "reacomp-standalone.dll": ("4c0862ab3cfd8a0345481b4792c07bf8d5a9761014f217d4e13669bf8143c7a0",
                               "https://www.reaper.fm/reaplugs/reaplugs236_x64-install.exe",
                               ("nsis", None, b"ReaComp")),
}

_cache = {}


def fetch(url):
    if url not in _cache:
        print(f"  downloading {url.split('/')[-1].split('?')[0].replace('%20', ' ')} ...", flush=True)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Mic Control plugin setup)"})
        with urllib.request.urlopen(req, timeout=120) as r:
            _cache[url] = r.read()
    return _cache[url]


def sha(b):
    return hashlib.sha256(b).hexdigest()


def is_vst2_x64(b):
    if b[:2] != b"MZ":
        return False
    pe = struct.unpack_from("<I", b, 0x3C)[0]
    return struct.unpack_from("<H", b, pe + 4)[0] == 0x8664 and b"VSTPluginMain" in b


def nsis_files(data):
    """Every file stored in an NSIS installer (its raw data blocks), without running it."""
    pos = data.find(b"NullsoftInst") - 8
    _, sig, magic, _, total = struct.unpack_from("<II12sII", data, pos)
    if sig != 0xDEADBEEF or magic != b"NullsoftInst":
        raise ValueError("not an NSIS installer")
    body = data[pos + 28: pos + total]

    def lzma_raw(buf):
        props = buf[0]
        lc, rest = props % 9, props // 9
        lp, pb = rest % 5, rest // 5
        dict_size = struct.unpack_from("<I", buf, 1)[0]
        return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[
            {"id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": lc, "lp": lp, "pb": pb}]).decompress(buf[5:])

    blocks = []
    if body[0] == 0x5D:  # solid: one LZMA stream of length-prefixed blocks
        stream = lzma_raw(body)
        i = 0
        while i + 4 <= len(stream):
            n = struct.unpack_from("<I", stream, i)[0]
            blocks.append(stream[i + 4: i + 4 + n])
            i += 4 + n
    else:  # each block compressed on its own (top bit set) or stored
        i = 0
        while i + 4 <= len(body):
            n = struct.unpack_from("<I", body, i)[0]
            raw = body[i + 4: i + 4 + (n & 0x7FFFFFFF)]
            if not raw:
                break
            try:
                if not n & 0x80000000:
                    blocks.append(raw)
                elif raw[0] == 0x5D:
                    blocks.append(lzma_raw(raw))
                elif raw[0] == 0x01 and raw[1] == 0x5D:
                    blocks.append(lzma_raw(raw[1:]))
                else:
                    blocks.append(zlib.decompressobj(-15).decompress(raw))
            except Exception:  # noqa: BLE001 - a block we can't read is just skipped
                blocks.append(b"")
            i += 4 + (n & 0x7FFFFFFF)
    return blocks


def innoextract_exe(work):
    exe = next(work.glob("innoextract*/innoextract.exe"), None)
    if not exe:
        with zipfile.ZipFile(io.BytesIO(fetch(INNOEXTRACT))) as z:
            z.extractall(work / "innoextract")
        exe = next((work / "innoextract").rglob("innoextract.exe"))
    return exe


def get(name, want, url, how, work):
    archive = fetch(url)
    kind = how[0]
    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            return z.read(how[1])
    if kind == "inno":  # zip -> Inno Setup installer -> unpacked by innoextract (not run)
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            setup = work / how[1]
            setup.write_bytes(z.read(how[1]))
        out = work / ("inno-" + name)
        subprocess.run([str(innoextract_exe(work)), "-s", "-d", str(out), str(setup)], check=True, capture_output=True)
        found = [f.read_bytes() for f in out.rglob("*.dll") if how[2].lower() in f.name.lower()]
        found = [b for b in found if is_vst2_x64(b)]
        return next((b for b in found if sha(b) == want), found[0] if found else None)
    if kind == "nsis":  # (zip ->) NSIS installer -> its stored files, read directly (not run)
        if how[1]:
            with zipfile.ZipFile(io.BytesIO(archive)) as z:
                archive = z.read(how[1])
        found = [b for b in nsis_files(archive) if is_vst2_x64(b)]
        exact = next((b for b in found if sha(b) == want), None)
        return exact or next((b for b in found if how[2] in b), None)
    raise ValueError(kind)


def main():
    force = "--force" in sys.argv
    dest = Path(sys.argv[sys.argv.index("--dest") + 1]) if "--dest" in sys.argv else DEST
    dest.mkdir(parents=True, exist_ok=True)
    ok, failed = [], []
    with tempfile.TemporaryDirectory(prefix="miccontrol-plugins-") as tmp:
        work = Path(tmp)
        for name, (want, url, how) in PLUGINS.items():
            target = dest / name
            if target.exists() and not force:
                ok.append(name)
                continue
            print(name, flush=True)
            try:
                data = get(name, want, url, how, work)
                if not data:
                    raise ValueError("the plugin wasn't found in the download")
                if sha(data) != want:
                    print(f"  note: a different version than the tested one (still installed)")
                target.write_bytes(data)
                print(f"  ok, {len(data) // 1024} KB")
                ok.append(name)
            except Exception as e:  # noqa: BLE001 - one failing site shouldn't stop the others
                print(f"  FAILED: {e}")
                failed.append((name, url))
    print(f"\n{len(ok)}/{len(PLUGINS)} plugins ready in {dest}")
    if failed:
        print("Couldn't get these (Mic Control works without them; try again later, or download by hand):")
        for name, url in failed:
            print(f"  {name}: {url}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
