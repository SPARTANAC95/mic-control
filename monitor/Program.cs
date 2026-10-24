// MicMonitor: lowest-latency "hear yourself" for Mic Control.
//
// Captures the mic endpoint in shared mode (Equalizer APO's effects are already applied: exactly what apps
// hear) and plays it to an output endpoint. A capture thread and a render thread, each woken by Windows
// when its device needs data, share an adaptive jitter buffer: it keeps only a small cushion (grown after a
// dropout, shrunk again when things are calm) and trims or stretches it with short crossfades, so two
// devices with different clocks never drift into extra delay. The output gets exactly one period at a time.
// Both threads run as "Pro Audio" (MMCSS). The output can run in exclusive mode at the hardware's minimum
// period (skips the Windows mixer).
//
//   MicMonitor --capture <endpoint id> [--render <endpoint id>|default] [--volume 0..1.5] [--exclusive]
//   MicMonitor --probe <render endpoint id>|default      -> periods the output supports (JSON)
//   MicMonitor --list                                    -> active outputs with their default roles (JSON)
// stdin: "v <volume>" changes the volume, "q" quits.  stdout: one JSON status line per second.

using System;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace MicMonitor;

static class Program
{
    static volatile float volume = 1f;
    static volatile bool quit;
    static readonly CultureInfo IC = CultureInfo.InvariantCulture;

    static int Main(string[] args)
    {
        try
        {
            if (Arg(args, "--probe") is string probe) { Console.WriteLine(Probe(probe)); return 0; }
            if (Array.IndexOf(args, "--list") >= 0) { Console.WriteLine(Audio.ListOutputs()); return 0; }
            string capId = Arg(args, "--capture"), renId = Arg(args, "--render");
            if (float.TryParse(Arg(args, "--volume"), NumberStyles.Float, IC, out var v)) volume = Math.Clamp(v, 0f, 1.5f);
            if (capId == null) { Console.WriteLine("{\"error\":\"--capture is required\"}"); return 2; }
            new Thread(ReadCommands) { IsBackground = true }.Start();
            bool exclusive = Array.IndexOf(args, "--exclusive") >= 0;
            for (int scale = 1; ; )
            {
                int rc = new Monitor(capId, renId, exclusive, scale).Run();
                if (rc != Monitor.TooFast || Quit) return rc;
                // the device accepted a buffer smaller than it really services: try 2x, 4x, then the shared mixer
                if (scale < 4) scale *= 2; else { exclusive = false; scale = 1; }
            }
        }
        catch (Exception e) { Console.WriteLine("{\"error\":" + Json(e.Message) + "}"); return 1; }
    }

    internal static string Arg(string[] a, string name) { int i = Array.IndexOf(a, name); return i >= 0 && i + 1 < a.Length ? a[i + 1] : null; }
    internal static string Json(string s) => "\"" + s.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\n", " ").Replace("\r", "") + "\"";
    internal static float Volume => volume;
    internal static bool Quit => quit;
    internal static void Stop() => quit = true;

    static void ReadCommands()
    {
        string line;
        while ((line = Console.ReadLine()) != null)
        {
            if (line == "q") break;
            if (line.StartsWith("v ") && float.TryParse(line[2..], NumberStyles.Float, IC, out var v)) volume = Math.Clamp(v, 0f, 1.5f);
        }
        quit = true;  // "q", or the parent went away
    }

    static string Probe(string id)
    {
        var dev = Audio.Device(id, render: true);
        var c = Audio.Activate(dev);
        Audio.Check(c.GetMixFormat(out var fmt), "format");
        var f = Marshal.PtrToStructure<WaveFormatEx>(fmt);
        c.GetDevicePeriod(out long defP, out long minP);
        int sharedMin = -1;
        if (c.GetSharedModeEnginePeriod(fmt, out _, out _, out uint mn, out _) == 0) sharedMin = (int)mn;
        bool excl = Audio.ExclusiveFormat(c, f.nSamplesPerSec, f.nChannels) != IntPtr.Zero;
        return string.Format(IC, "{{\"rate\":{0},\"channels\":{1},\"sharedPeriodMs\":{2:0.00},\"sharedMinMs\":{3:0.00},\"exclusive\":{4},\"exclusiveMinMs\":{5:0.00}}}",
            f.nSamplesPerSec, f.nChannels, defP / 10000.0, sharedMin > 0 ? sharedMin * 1000.0 / f.nSamplesPerSec : defP / 10000.0,
            excl ? "true" : "false", minP / 10000.0);
    }
}

sealed class Monitor
{
    readonly string capId, renId;
    readonly bool exclusive;
    readonly int scale;
    public const int TooFast = 3;  // Run() result: exclusive buffer too small for this device's real cadence
    volatile bool stop;
    float[] ring;                  // interleaved output-channel samples
    int ringFrames, outCh, rate;
    long writePos, readPos;        // in frames; capture thread writes, render thread reads
    readonly object ringLock = new();
    int capPacket, renPeriod;
    string mode = "shared", fatal;
    volatile bool tooFast;

    // jitter buffer: aim for `margin` frames left over at the low point of each half-second window
    const int XF = 64;             // crossfade length for trims/stretches (1.3 ms)
    int margin, minMargin, maxMargin;
    long minFill = long.MaxValue, windowFrames, calmFrames, framesOut;
    bool playing;
    int fadeLeft; long fadeOld;    // after a splice: frames still fading out from the old continuation
    long pending;                  // splice decided by Adapt, done at the next pull (+ skip ahead, - replay)
    long trimmed, stretched, underruns, late;
    // mic-side health: packets Windows flagged as broken, frames missing from the stream, longest wait for a packet
    long micGlitches, micMissing; ulong nextPos; double micMaxWait;
    double padSum, lowSum; long padCount, lowCount;
    float peak;

    public Monitor(string capId, string renId, bool exclusive, int scale)
    { this.capId = capId; this.renId = renId; this.exclusive = exclusive; this.scale = scale; }

    bool Running => !Program.Quit && !stop;

    public int Run()
    {
        var cap = Audio.Activate(Audio.Device(capId, render: false));
        var ren = Audio.Activate(Audio.Device(renId, render: true));
        Audio.Check(cap.GetMixFormat(out var capFmtPtr), "mic format");
        var capFmt = Marshal.PtrToStructure<WaveFormatEx>(capFmtPtr);
        rate = (int)capFmt.nSamplesPerSec;
        int inCh = capFmt.nChannels;
        bool inFloat = Audio.IsFloat(capFmtPtr);
        int inBits = capFmt.wBitsPerSample;
        minMargin = rate / 1000; maxMargin = rate * 12 / 1000; margin = rate * 2 / 1000;

        // ---- capture: shared (so the effects apply), smallest period Windows offers
        var capEvt = new AutoResetEvent(false);
        capPacket = Audio.InitShared(cap, capFmtPtr, convert: false, headroomHns: 1_000_000);  // 100 ms headroom
        Audio.Check(cap.SetEventHandle(capEvt.SafeWaitHandle.DangerousGetHandle()), "mic event");

        // ---- render: exclusive at the hardware minimum if asked and possible, else shared
        var renEvt = new AutoResetEvent(false);
        IntPtr renFmtPtr;
        if (exclusive && (renFmtPtr = Audio.InitExclusive(ref ren, renId, (uint)rate, scale, out renPeriod)) != IntPtr.Zero)
            mode = "exclusive";
        else
        {
            Audio.Check(ren.GetMixFormat(out var mix), "output format");
            var mf = Marshal.PtrToStructure<WaveFormatEx>(mix);
            bool convert = mf.nSamplesPerSec != rate;  // different rate: render in the mic's format, Windows converts
            renFmtPtr = convert ? capFmtPtr : mix;
            renPeriod = Audio.InitShared(ren, renFmtPtr, convert);
        }
        Audio.Check(ren.SetEventHandle(renEvt.SafeWaitHandle.DangerousGetHandle()), "output event");
        var rf = Marshal.PtrToStructure<WaveFormatEx>(renFmtPtr);
        outCh = rf.nChannels;
        bool renFloat = Audio.IsFloat(renFmtPtr); int renBits = rf.wBitsPerSample;
        Audio.Check(ren.GetBufferSize(out uint renBuf), "output buffer");

        ringFrames = Math.Max(16384, capPacket * 16);
        ring = new float[ringFrames * outCh];

        var capGuid = typeof(IAudioCaptureClient).GUID; var renGuid = typeof(IAudioRenderClient).GUID;
        Audio.Check(cap.GetService(ref capGuid, out var cs), "mic service");
        Audio.Check(ren.GetService(ref renGuid, out var rs), "output service");
        var capSvc = (IAudioCaptureClient)cs; var renSvc = (IAudioRenderClient)rs;

        // render thread: on each device event top the output up to exactly one period
        var renThread = new Thread(() =>
        {
            try
            {
                using var mm = new ProAudio();
                var buf = new float[renBuf * outCh];
                long lastWake = 0, windowStart = Stopwatch.GetTimestamp(), lateInWindow = 0, windowOut = 0;
                double tickMs = 1000.0 / Stopwatch.Frequency, periodMs = renPeriod * 1000.0 / rate;
                Audio.Check(ren.Start(), "start output");
                while (Running)
                {
                    if (!renEvt.WaitOne(200)) continue;
                    long now = Stopwatch.GetTimestamp();
                    if (lastWake != 0 && framesOut > rate / 4 && (now - lastWake) * tickMs > periodMs * 1.8) { late++; lateInWindow++; }
                    lastWake = now;
                    if (framesOut <= rate / 4) { windowStart = now; windowOut = 0; }
                    else if ((now - windowStart) * tickMs >= 1500)
                    {
                        // an exclusive device must take audio at the real rate; if it services our buffer less
                        // often than it claims (it is glitching), step up to a bigger buffer
                        double expected = rate * (now - windowStart) * tickMs / 1000;
                        if (Environment.GetEnvironmentVariable("MICMONITOR_DEBUG") == "1")
                            Console.WriteLine($"{{\"debug\":\"period {periodMs:0.00} ms, took {windowOut / expected:P1} of real time, late {lateInWindow}\"}}");
                        if (mode == "exclusive" && (windowOut < expected * 0.95 || lateInWindow >= 8)) { tooFast = true; stop = true; break; }
                        windowStart = now; lateInWindow = 0; windowOut = 0;
                    }
                    uint pad = 0, want;
                    if (mode == "exclusive") want = renBuf;
                    else
                    {
                        Audio.Check(ren.GetCurrentPadding(out pad), "output padding");
                        if (pad >= renPeriod) continue;
                        want = Math.Min((uint)renPeriod - pad, renBuf - pad);
                    }
                    int got = Pull(buf, (int)want, pad);
                    if (got < want) Array.Clear(buf, got * outCh, ((int)want - got) * outCh);
                    Audio.Check(renSvc.GetBuffer(want, out IntPtr p), "output buffer");
                    for (int i = 0; i < want * outCh; i++) Audio.Write(p, i, buf[i], renFloat, renBits);
                    renSvc.ReleaseBuffer(want, 0);
                    framesOut += want; windowOut += want;
                }
                ren.Stop();
            }
            catch (Exception e) { fatal = e.Message; stop = true; }
        }) { IsBackground = true, Priority = ThreadPriority.Highest };

        // capture (this thread): push packets into the ring
        using var mmCap = new ProAudio();
        if (mode == "exclusive")  // exclusive output needs a full first buffer before Start
        {
            renSvc.GetBuffer(renBuf, out _); renSvc.ReleaseBuffer(renBuf, 2 /*silent*/);
        }
        renThread.Start();
        Audio.Check(cap.Start(), "start mic");
        var tmp = new float[Math.Max(capPacket, 4096) * 4 * outCh];
        float vol = Program.Volume;
        long lastReport = Environment.TickCount64, lastPacket = 0;
        try
        {
            while (Running)
            {
                if (capEvt.WaitOne(200))
                {
                    while (true)
                    {
                        Audio.Check(capSvc.GetNextPacketSize(out uint packet), "mic packet");
                        if (packet == 0) break;
                        Audio.Check(capSvc.GetBuffer(out IntPtr data, out uint n, out uint flags, out ulong pos, out _), "mic buffer");
                        long nowTick = Stopwatch.GetTimestamp();
                        if (lastPacket != 0) micMaxWait = Math.Max(micMaxWait, (nowTick - lastPacket) * 1000.0 / Stopwatch.Frequency);
                        lastPacket = nowTick;
                        if ((flags & 1) != 0 && framesOut > 0) micGlitches++;
                        if (nextPos != 0 && pos > nextPos) micMissing += (long)(pos - nextPos);
                        nextPos = pos + n;
                        if (tmp.Length < n * outCh) tmp = new float[n * outCh];
                        bool silent = (flags & 2) != 0;
                        float target = Program.Volume, pk = 0;
                        for (int f = 0; f < n; f++)
                        {
                            vol += (target - vol) * 0.003f;  // ~7 ms glide: no zipper noise on volume changes
                            for (int c = 0; c < outCh; c++)
                            {
                                float s = silent ? 0f : Audio.Read(data, f * inCh + Math.Min(c, inCh - 1), inFloat, inBits) * vol;
                                tmp[f * outCh + c] = s;
                                if (s > pk) pk = s; else if (-s > pk) pk = -s;
                            }
                        }
                        capSvc.ReleaseBuffer(n);
                        Push(tmp, (int)n);
                        if (pk > peak) peak = pk;
                    }
                }
                if (Environment.TickCount64 - lastReport >= 1000) { lastReport = Environment.TickCount64; Report(cap, ren); }
            }
        }
        catch (Exception e) { fatal ??= e.Message; stop = true; }
        stop = true;
        cap.Stop();
        renThread.Join(1000);
        foreach (var o in new object[] { capSvc, renSvc, cap, ren }) try { Marshal.FinalReleaseComObject(o); } catch { }
        if (tooFast && fatal == null) return TooFast;
        if (fatal != null) { Console.WriteLine("{\"error\":" + Program.Json(fatal) + "}"); return 1; }
        return 0;
    }

    void Push(float[] src, int frames)
    {
        lock (ringLock)
        {
            for (int f = 0; f < frames; f++)
            {
                Array.Copy(src, f * outCh, ring, (int)(writePos % ringFrames) * outCh, outCh);
                writePos++;
            }
            // safety valve (output stalled): never let more than 100 ms pile up
            long keep = margin + capPacket;
            if (writePos - readPos > rate / 10) { trimmed += writePos - readPos - keep; readPos = writePos - keep; fadeLeft = 0; }
        }
    }

    int Pull(float[] dst, int frames, uint pad)
    {
        lock (ringLock)
        {
            long avail = writePos - readPos;
            if (!playing) { if (avail < capPacket / 2) return 0; playing = true; }
            if (pending != 0 && fadeLeft == 0)
            {
                if (pending > 0 ? avail >= pending + XF : avail >= XF && writePos - (readPos + pending) < ringFrames)
                {
                    fadeOld = readPos; readPos += pending; fadeLeft = XF;
                    if (pending > 0) trimmed += pending; else stretched -= pending;
                    avail = writePos - readPos;
                }
                pending = 0;
            }
            int n = (int)Math.Min(frames, avail);
            for (int f = 0; f < n; f++)
            {
                int at = (int)(readPos % ringFrames) * outCh, o = f * outCh;
                if (fadeLeft > 0)
                {
                    float g = fadeLeft / (float)XF;
                    int old = (int)(fadeOld % ringFrames) * outCh;
                    for (int c = 0; c < outCh; c++) dst[o + c] = ring[at + c] * (1 - g) + ring[old + c] * g;
                    fadeLeft--; fadeOld++;
                }
                else Array.Copy(ring, at, dst, o, outCh);
                readPos++;
            }
            if (n < frames && framesOut > rate)  // ran dry: a dropout; keep a bigger cushion from now on
            {
                underruns++; margin = Math.Min(maxMargin, margin + rate * 3 / 1000); calmFrames = 0;  // +3 ms: one dropout is enough to adapt
                playing = false; fadeLeft = 0;
            }
            long fill = writePos - readPos;
            if (fill < minFill) minFill = fill;
            padSum += pad + frames; padCount++;
            windowFrames += frames;
            if (windowFrames >= rate / 2) Adapt();
            return n;
        }
    }

    // Every half second: move the low point of the buffer to `margin` with one crossfaded splice.
    void Adapt()
    {
        if (playing && minFill != long.MaxValue) { lowSum += minFill; lowCount++; }
        if (playing && minFill != long.MaxValue)
        {
            if (minFill > margin + rate / 2000) pending = minFill - margin;       // waiting too long: skip ahead
            else if (minFill < margin / 2) pending = minFill - margin;            // cutting it close: replay a moment
        }
        calmFrames += windowFrames;
        if (calmFrames >= rate * 45L && margin > minMargin) { margin = Math.Max(minMargin, margin - rate / 2000); calmFrames = 0; }
        minFill = long.MaxValue; windowFrames = 0;
    }

    void Report(IAudioClient3 cap, IAudioClient3 ren)
    {
        double low, pad; long tr, st, un;
        lock (ringLock)
        {
            low = lowCount > 0 ? lowSum / lowCount : margin; pad = padCount > 0 ? padSum / padCount : renPeriod;
            padSum = 0; padCount = 0; tr = trimmed; st = stretched; un = underruns;
        }
        cap.GetStreamLatency(out long cl); ren.GetStreamLatency(out long rl);
        double ms(double fr) => fr * 1000.0 / rate;
        // Every sample waits one mic packet plus the buffer's low point (the first sample of a packet waits
        // for the whole packet, the last one for the packets queued ahead of it), then the output queue:
        // exclusive = the hardware buffer; shared = our queue + one pass of the Windows mixer.
        double output = mode == "exclusive" ? ms(renPeriod) : ms(pad) + ms(renPeriod);
        double drivers = (cl + rl) / 10000.0;
        double total = ms(capPacket) + ms(low) + output + drivers;
        Console.WriteLine(string.Format(CultureInfo.InvariantCulture,
            "{{\"mode\":\"{0}\",\"rate\":{1},\"micPeriodMs\":{2:0.00},\"outputPeriodMs\":{3:0.00},\"bufferMs\":{4:0.00},\"outputMs\":{5:0.00},\"driverMs\":{6:0.0},\"latencyMs\":{7:0.0},\"cushionMs\":{8:0.0},\"trimmedMs\":{9:0},\"stretchedMs\":{10:0},\"underruns\":{11},\"late\":{12},\"peak\":{13:0.000},\"micGlitches\":{14},\"micMissingMs\":{15:0.0},\"micMaxWaitMs\":{16:0.0},\"mmcss\":\"{17}/{18}\"}}",
            mode, rate, ms(capPacket), ms(renPeriod), ms(low), output, drivers, total, ms(margin),
            ms(tr), ms(st), un, Interlocked.Read(ref late), peak, micGlitches, ms(micMissing), micMaxWait, ProAudio.Granted, ProAudio.Granted + ProAudio.Refused));
        micMaxWait = 0;
        peak = 0;
    }
}

sealed class ProAudio : IDisposable
{
    readonly IntPtr h;
    public static int Granted, Refused;  // reported in the status: did Windows give our threads audio priority?
    public ProAudio()
    {
        uint i = 0; h = AvSetMmThreadCharacteristicsW("Pro Audio", ref i);
        if (h != IntPtr.Zero) { AvSetMmThreadPriority(h, 2); Interlocked.Increment(ref Granted); }
        else Interlocked.Increment(ref Refused);
    }
    public void Dispose() { if (h != IntPtr.Zero) AvRevertMmThreadCharacteristics(h); }
    [DllImport("avrt.dll", CharSet = CharSet.Unicode)] static extern IntPtr AvSetMmThreadCharacteristicsW(string task, ref uint index);
    [DllImport("avrt.dll")] static extern bool AvSetMmThreadPriority(IntPtr h, int priority);
    [DllImport("avrt.dll")] static extern bool AvRevertMmThreadCharacteristics(IntPtr h);
}

static class Audio
{
    const uint EVENT = 0x00040000, AUTOCONVERT = 0x80000000, SRC_DEFAULT = 0x08000000;
    const int BUFFER_NOT_ALIGNED = unchecked((int)0x88890019);

    public static void Check(int hr, string what) { if (hr != 0) throw new Exception($"{what} failed (0x{hr:X8})"); }

    public static IMMDevice Device(string id, bool render)
    {
        var en = (IMMDeviceEnumerator)new MMDeviceEnumerator();
        IMMDevice d;
        if (render && (string.IsNullOrEmpty(id) || id == "default")) Check(en.GetDefaultAudioEndpoint(0, 1, out d), "default output");
        else Check(en.GetDevice(id, out d), render ? "output device" : "mic device");
        return d;
    }

    public static string ListOutputs()
    {
        var en = (IMMDeviceEnumerator)new MMDeviceEnumerator();
        string Default(int role) { if (en.GetDefaultAudioEndpoint(0, role, out var d) != 0) return ""; d.GetId(out var id); return id; }
        string media = Default(1), comms = Default(2);
        Check(en.EnumAudioEndpoints(0, 1 /*active*/, out var list), "list outputs");
        list.GetCount(out uint count);
        var nameKey = new PropertyKey { fmtid = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0"), pid = 14 };
        var sb = new StringBuilder("[");
        for (uint i = 0; i < count; i++)
        {
            if (list.Item(i, out var d) != 0) continue;
            d.GetId(out var id);
            string name = id;
            if (d.OpenPropertyStore(0, out var props) == 0 && props.GetValue(ref nameKey, out var v) == 0)
            {
                if (v.vt == 31 && v.p != IntPtr.Zero) name = Marshal.PtrToStringUni(v.p);
                PropVariantClear(ref v);
            }
            if (sb.Length > 1) sb.Append(',');
            sb.Append("{\"id\":").Append(Program.Json(id)).Append(",\"name\":").Append(Program.Json(name))
              .Append(",\"default\":").Append(id == media ? "true" : "false")
              .Append(",\"comms\":").Append(id == comms ? "true" : "false").Append('}');
        }
        return sb.Append(']').ToString();
    }

    [DllImport("ole32.dll")] static extern int PropVariantClear(ref PropVariant v);

    public static IAudioClient3 Activate(IMMDevice d)
    {
        var iid = typeof(IAudioClient3).GUID;
        Check(d.Activate(ref iid, 1, IntPtr.Zero, out var o), "activate");
        return (IAudioClient3)o;
    }

    // Shared stream at the smallest period the device allows (IAudioClient3) when that's actually smaller than
    // the classic engine period; otherwise the classic stream, with `headroomHns` of buffer. Headroom adds no
    // delay (data is taken on every event), it only gives the reader time if the PC is busy: a game at full
    // load can hold a thread back past a minimal buffer, and Windows then drops audio.
    public static int InitShared(IAudioClient3 c, IntPtr fmt, bool convert, long headroomHns = 0)
    {
        if (!convert && c.GetSharedModeEnginePeriod(fmt, out uint def, out _, out uint min, out _) == 0 && min < def
            && c.InitializeSharedAudioStream(EVENT, min, fmt, IntPtr.Zero) == 0)
            return (int)min;
        Check(c.Initialize(0, EVENT | (convert ? AUTOCONVERT | SRC_DEFAULT : 0), headroomHns, 0, fmt, IntPtr.Zero), "stream");
        c.GetDevicePeriod(out long p, out _);
        var f = Marshal.PtrToStructure<WaveFormatEx>(fmt);
        return (int)Math.Round(p * f.nSamplesPerSec / 1e7);
    }

    // Exclusive output at the hardware minimum period. Returns the format used, or Zero if not possible.
    public static IntPtr InitExclusive(ref IAudioClient3 c, string id, uint rate, int scale, out int periodFrames)
    {
        periodFrames = 0;
        c.GetMixFormat(out var mix);
        var mf = Marshal.PtrToStructure<WaveFormatEx>(mix);
        IntPtr fmt = ExclusiveFormat(c, rate, mf.nChannels);
        if (fmt == IntPtr.Zero) return IntPtr.Zero;
        c.GetDevicePeriod(out long defP, out long minP);
        minP = Math.Min(minP * scale, Math.Max(defP, minP));
        int hr = c.Initialize(1 /*exclusive*/, EVENT, minP, minP, fmt, IntPtr.Zero);
        if (hr == BUFFER_NOT_ALIGNED)  // the driver wants a slightly different size: use the one it offers
        {
            c.GetBufferSize(out uint frames);
            minP = (long)Math.Round(1e7 * frames / rate);
            c = Activate(Device(id, render: true));
            hr = c.Initialize(1, EVENT, minP, minP, fmt, IntPtr.Zero);
        }
        if (hr != 0) { c = Activate(Device(id, render: true)); return IntPtr.Zero; }
        c.GetBufferSize(out uint buf);
        periodFrames = (int)buf;
        return fmt;
    }

    public static IntPtr ExclusiveFormat(IAudioClient3 c, uint rate, int channels)
    {
        // float32, int32 (24 valid), packed 24, int16 - the first one the driver accepts exclusively
        foreach (var (bits, valid, isFloat) in new[] { (32, 32, true), (32, 24, false), (24, 24, false), (16, 16, false) })
        {
            IntPtr p = Marshal.AllocHGlobal(40);
            var w = new WaveFormatExtensible
            {
                wFormatTag = 0xFFFE, nChannels = (ushort)channels, nSamplesPerSec = rate, wBitsPerSample = (ushort)bits,
                nBlockAlign = (ushort)(channels * bits / 8), cbSize = 22, wValidBitsPerSample = (ushort)valid,
                dwChannelMask = channels == 2 ? 3u : 0u,
                SubFormat = isFloat ? new Guid("00000003-0000-0010-8000-00aa00389b71") : new Guid("00000001-0000-0010-8000-00aa00389b71"),
            };
            w.nAvgBytesPerSec = rate * w.nBlockAlign;
            Marshal.StructureToPtr(w, p, false);
            if (c.IsFormatSupported(1, p, out _) == 0) return p;
            Marshal.FreeHGlobal(p);
        }
        return IntPtr.Zero;
    }

    public static bool IsFloat(IntPtr fmt)
    {
        var f = Marshal.PtrToStructure<WaveFormatEx>(fmt);
        if (f.wFormatTag == 3) return true;
        return f.wFormatTag == 0xFFFE && Marshal.PtrToStructure<Guid>(fmt + 24) == new Guid("00000003-0000-0010-8000-00aa00389b71");
    }

    public static unsafe float Read(IntPtr p, int i, bool isFloat, int bits)
    {
        if (isFloat) return ((float*)p)[i];
        if (bits == 16) return ((short*)p)[i] / 32768f;
        if (bits == 32) return ((int*)p)[i] / 2147483648f;
        byte* b = (byte*)p + i * 3; return ((b[0] << 8 | b[1] << 16 | b[2] << 24) >> 8) / 8388608f;
    }

    public static unsafe void Write(IntPtr p, int i, float s, bool isFloat, int bits)
    {
        if (s > 1f) s = 1f; else if (s < -1f) s = -1f;
        if (isFloat) ((float*)p)[i] = s;
        else if (bits == 16) ((short*)p)[i] = (short)(s * 32767);
        else if (bits == 32) ((int*)p)[i] = (int)(s * 2147483647.0);
        else { int v = (int)(s * 8388607); byte* b = (byte*)p + i * 3; b[0] = (byte)v; b[1] = (byte)(v >> 8); b[2] = (byte)(v >> 16); }
    }
}

[StructLayout(LayoutKind.Sequential, Pack = 2)]
struct WaveFormatEx { public ushort wFormatTag, nChannels; public uint nSamplesPerSec, nAvgBytesPerSec; public ushort nBlockAlign, wBitsPerSample, cbSize; }

[StructLayout(LayoutKind.Sequential, Pack = 2)]
struct WaveFormatExtensible
{
    public ushort wFormatTag, nChannels; public uint nSamplesPerSec, nAvgBytesPerSec; public ushort nBlockAlign, wBitsPerSample, cbSize;
    public ushort wValidBitsPerSample; public uint dwChannelMask; public Guid SubFormat;
}

[StructLayout(LayoutKind.Sequential)] struct PropertyKey { public Guid fmtid; public int pid; }
[StructLayout(LayoutKind.Sequential)] struct PropVariant { public ushort vt, r1, r2, r3; public IntPtr p, p2; }

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumerator { }

[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator
{
    [PreserveSig] int EnumAudioEndpoints(int dataFlow, int stateMask, out IMMDeviceCollection devices);
    [PreserveSig] int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device);
    [PreserveSig] int GetDevice([MarshalAs(UnmanagedType.LPWStr)] string id, out IMMDevice device);
}

[ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceCollection
{
    [PreserveSig] int GetCount(out uint count);
    [PreserveSig] int Item(uint index, out IMMDevice device);
}

[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice
{
    [PreserveSig] int Activate(ref Guid iid, int clsCtx, IntPtr activationParams, [MarshalAs(UnmanagedType.IUnknown)] out object iface);
    [PreserveSig] int OpenPropertyStore(int access, out IPropertyStore props);
    [PreserveSig] int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
    [PreserveSig] int GetState(out int state);
}

[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPropertyStore
{
    [PreserveSig] int GetCount(out uint count);
    [PreserveSig] int GetAt(uint index, out PropertyKey key);
    [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant value);
}

[ComImport, Guid("7ED4EE07-8E67-4CD4-8C1A-2B7A5987AD42"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioClient3
{
    [PreserveSig] int Initialize(int shareMode, uint streamFlags, long bufferDuration, long periodicity, IntPtr format, IntPtr sessionGuid);
    [PreserveSig] int GetBufferSize(out uint frames);
    [PreserveSig] int GetStreamLatency(out long latency);
    [PreserveSig] int GetCurrentPadding(out uint frames);
    [PreserveSig] int IsFormatSupported(int shareMode, IntPtr format, out IntPtr closest);
    [PreserveSig] int GetMixFormat(out IntPtr format);
    [PreserveSig] int GetDevicePeriod(out long defaultPeriod, out long minimumPeriod);
    [PreserveSig] int Start();
    [PreserveSig] int Stop();
    [PreserveSig] int Reset();
    [PreserveSig] int SetEventHandle(IntPtr handle);
    [PreserveSig] int GetService(ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object service);
    [PreserveSig] int IsOffloadCapable(int category, out int capable);
    [PreserveSig] int SetClientProperties(IntPtr props);
    [PreserveSig] int GetBufferSizeLimits(IntPtr format, int eventDriven, out long min, out long max);
    [PreserveSig] int GetSharedModeEnginePeriod(IntPtr format, out uint defaultPeriod, out uint fundamentalPeriod, out uint minPeriod, out uint maxPeriod);
    [PreserveSig] int GetCurrentSharedModeEnginePeriod(out IntPtr format, out uint currentPeriod);
    [PreserveSig] int InitializeSharedAudioStream(uint streamFlags, uint periodInFrames, IntPtr format, IntPtr sessionGuid);
}

[ComImport, Guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioCaptureClient
{
    [PreserveSig] int GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devicePosition, out ulong qpcPosition);
    [PreserveSig] int ReleaseBuffer(uint frames);
    [PreserveSig] int GetNextPacketSize(out uint frames);
}

[ComImport, Guid("F294ACFC-3146-4483-A7BF-ADDCA7C260E2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioRenderClient
{
    [PreserveSig] int GetBuffer(uint frames, out IntPtr data);
    [PreserveSig] int ReleaseBuffer(uint frames, uint flags);
}
