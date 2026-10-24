"""Minimal VST2 host over ctypes: list parameters, map normalized values to display text,
read chunks, and run audio through a plugin. Used to calibrate plugins for Mic Control."""
import ctypes as C
import math
import sys

intptr = C.c_ssize_t
AudioMaster = C.CFUNCTYPE(intptr, C.c_void_p, C.c_int32, C.c_int32, intptr, C.c_void_p, C.c_float)
RATE = 48000


class AEffect(C.Structure):
    _fields_ = [
        ("magic", C.c_int32),
        ("dispatcher", C.c_void_p), ("process", C.c_void_p),
        ("setParameter", C.c_void_p), ("getParameter", C.c_void_p),
        ("numPrograms", C.c_int32), ("numParams", C.c_int32),
        ("numInputs", C.c_int32), ("numOutputs", C.c_int32), ("flags", C.c_int32),
        ("resvd1", intptr), ("resvd2", intptr),
        ("initialDelay", C.c_int32), ("realQualities", C.c_int32), ("offQualities", C.c_int32), ("ioRatio", C.c_float),
        ("object", C.c_void_p), ("user", C.c_void_p),
        ("uniqueID", C.c_int32), ("version", C.c_int32),
        ("processReplacing", C.c_void_p), ("processDoubleReplacing", C.c_void_p),
        ("future", C.c_char * 56),
    ]


Dispatcher = C.CFUNCTYPE(intptr, C.POINTER(AEffect), C.c_int32, C.c_int32, intptr, C.c_void_p, C.c_float)
SetParam = C.CFUNCTYPE(None, C.POINTER(AEffect), C.c_int32, C.c_float)
GetParam = C.CFUNCTYPE(C.c_float, C.POINTER(AEffect), C.c_int32)
Process = C.CFUNCTYPE(None, C.POINTER(AEffect), C.POINTER(C.POINTER(C.c_float)), C.POINTER(C.POINTER(C.c_float)), C.c_int32)


@AudioMaster
def audio_master(effect, opcode, index, value, ptr, opt):
    if opcode == 1:   # audioMasterVersion
        return 2400
    if opcode == 16:  # audioMasterGetSampleRate
        return RATE
    if opcode == 17:  # audioMasterGetBlockSize
        return 512
    return 0


class Plugin:
    def __init__(self, path, rate=RATE, block=512):
        self.lib = C.WinDLL(path)
        main = C.CFUNCTYPE(C.POINTER(AEffect), AudioMaster)(("VSTPluginMain", self.lib))
        self.eff = main(audio_master)
        e = self.eff.contents
        assert e.magic == 0x56737450, "not a VST2 plugin"
        self._d = Dispatcher(e.dispatcher)
        self._set = SetParam(e.setParameter)
        self._get = GetParam(e.getParameter)
        self._proc = Process(e.processReplacing)
        self.n = e.numParams
        self.ins, self.outs = e.numInputs, e.numOutputs
        self.call(0)                    # effOpen
        self.call(10, opt=float(rate))  # effSetSampleRate
        self.call(11, value=block)      # effSetBlockSize
        self.call(12, value=1)          # effMainsChanged on
        self.call(71)                   # effStartProcess
        self.block = block

    @property
    def latency(self):
        return self.eff.contents.initialDelay

    @property
    def chunks(self):
        return bool(self.eff.contents.flags & 32)

    def call(self, op, index=0, value=0, ptr=None, opt=0.0):
        return self._d(self.eff, op, index, value, ptr, opt)

    def text(self, op, index=0):
        buf = C.create_string_buffer(512)
        self.call(op, index, 0, C.cast(buf, C.c_void_p))
        return buf.value.decode("latin-1").strip()

    def name(self, i): return self.text(8, i)
    def label(self, i): return self.text(6, i)
    def display(self, i): return self.text(7, i)
    def get(self, i): return self._get(self.eff, i)
    def set(self, i, v): self._set(self.eff, i, C.c_float(v))

    def index(self, name):
        for i in range(self.n):
            if self.name(i) == name:
                return i
        raise KeyError(name)

    def chunk(self):
        ptr = C.c_void_p()
        n = self.call(23, 0, 0, C.byref(ptr))
        return C.string_at(ptr, n) if n > 0 and ptr.value else b""

    def run(self, left, right):
        n = len(left)
        out_l, out_r = [], []
        nin, nout = max(self.ins, 2), max(self.outs, 2)
        for start in range(0, n, self.block):
            m = min(self.block, n - start)
            ins = [(C.c_float * m)(*left[start:start + m]), (C.c_float * m)(*right[start:start + m])]
            ins += [(C.c_float * m)() for _ in range(nin - 2)]
            outs = [(C.c_float * m)() for _ in range(nout)]
            pin = (C.POINTER(C.c_float) * nin)(*[C.cast(a, C.POINTER(C.c_float)) for a in ins])
            pout = (C.POINTER(C.c_float) * nout)(*[C.cast(a, C.POINTER(C.c_float)) for a in outs])
            self._proc(self.eff, pin, pout, m)
            out_l += list(outs[0])
            out_r += list(outs[1] if nout > 1 else outs[0])
        return out_l, out_r


def dump(path, samples=(0.0, 0.25, 0.5, 0.75, 1.0)):
    p = Plugin(path)
    print(f"== {path.split(chr(92))[-1]}: product={p.text(48)!r} vendor={p.text(47)!r} params={p.n} "
          f"io={p.ins}/{p.outs} chunks={p.chunks} latency={p.latency} samples")
    for i in range(p.n):
        default = p.get(i)
        dd = p.display(i)
        vals = []
        for v in samples:
            p.set(i, v)
            vals.append(f"{v:g}={p.display(i)}")
        p.set(i, default)
        print(f"   [{i:2}] {p.name(i)!r:24} {p.label(i)!r:6} default={default:.4f} ({dd}) | " + "  ".join(vals))
    return p


if __name__ == "__main__":
    for path in sys.argv[1:]:
        dump(path)
