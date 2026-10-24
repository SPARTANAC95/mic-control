// MicTray: Mic Control's tray icon. Started by the engine (server.py), which it talks to over the local API.
//
// - click: show the Mic Control window (or open one); minimizing the window hides it into the tray
// - menu: Open, Mute mic, Monitor, Quit
// - the icon turns red while the mic is muted and shows a green dot while the monitor plays
// It exits when its stdin closes (the engine went away), so it can never outlive the engine.
//
//   MicTray --port 47820

using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace MicTray;

static class Program
{
    [STAThread]
    static void Main(string[] args)
    {
        int port = 47820;
        int i = Array.IndexOf(args, "--port");
        if (i >= 0 && i + 1 < args.Length) int.TryParse(args[i + 1], out port);
        ApplicationConfiguration.Initialize();
        using var tray = new Tray(port);
        // the engine holds our stdin; when it closes, the engine is gone and so are we
        new Thread(() => { try { while (Console.In.ReadLine() != null) { } } catch { } tray.Quit(); }) { IsBackground = true }.Start();
        Application.Run();
    }
}

sealed class Tray : IDisposable
{
    const string Title = "Mic Control";
    readonly NotifyIcon icon;
    readonly HttpClient http;
    readonly System.Windows.Forms.Timer poll, watch;
    readonly ToolStripMenuItem muteItem, monitorItem;
    readonly SynchronizationContext ui;
    Icon live, muted, liveMon, mutedMon;
    bool isMuted, monitorOn, busy;
    IntPtr hiddenWindow;

    public Tray(int port)
    {
        ui = SynchronizationContext.Current ?? new WindowsFormsSynchronizationContext();
        http = new HttpClient { BaseAddress = new Uri($"http://127.0.0.1:{port}/"), Timeout = TimeSpan.FromSeconds(3) };
        http.DefaultRequestHeaders.Add("X-Mic-Control", "1");
        live = Draw(Color.White, false); muted = Draw(Color.FromArgb(255, 38, 67), false);
        liveMon = Draw(Color.White, true); mutedMon = Draw(Color.FromArgb(255, 38, 67), true);

        var menu = new ContextMenuStrip { ShowImageMargin = false };
        menu.Items.Add(new ToolStripMenuItem("Open Mic Control", null, (_, _) => ShowWindow()) { Font = new Font(menu.Font, FontStyle.Bold) });
        muteItem = new ToolStripMenuItem("Mute mic", null, async (_, _) => await Post("api/mixer", $"{{\"micMute\":{(!isMuted).ToString().ToLower()}}}"));
        monitorItem = new ToolStripMenuItem("Monitor (hear yourself)", null, async (_, _) => await Post("api/monitor", $"{{\"on\":{(!monitorOn).ToString().ToLower()}}}"));
        menu.Items.Add(muteItem);
        menu.Items.Add(monitorItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(new ToolStripMenuItem("Quit Mic Control", null, async (_, _) => { await Post("api/quit", "{}"); Quit(); }));

        icon = new NotifyIcon { Icon = live, Text = Title, Visible = true, ContextMenuStrip = menu };
        icon.MouseClick += (_, e) => { if (e.Button == MouseButtons.Left) ShowWindow(); };

        poll = new System.Windows.Forms.Timer { Interval = 2000 };
        poll.Tick += async (_, _) => await Refresh();
        poll.Start();
        _ = Refresh();
        // minimize = into the tray: hide the minimized app window so it leaves the taskbar
        watch = new System.Windows.Forms.Timer { Interval = 400 };
        watch.Tick += (_, _) =>
        {
            IntPtr w = Win.FindApp(Title);
            if (w != IntPtr.Zero && Win.IsIconic(w) && Win.IsWindowVisible(w)) { Win.ShowWindow(w, Win.SW_HIDE); hiddenWindow = w; }
        };
        watch.Start();
    }

    void ShowWindow()
    {
        IntPtr w = hiddenWindow != IntPtr.Zero && Win.IsWindow(hiddenWindow) ? hiddenWindow : Win.FindApp(Title);
        hiddenWindow = IntPtr.Zero;
        if (w != IntPtr.Zero)
        {
            Win.ShowWindow(w, Win.SW_SHOW);
            Win.ShowWindow(w, Win.SW_RESTORE);
            Win.SetForegroundWindow(w);
        }
        else _ = Post("api/open-window", "{}");
    }

    async Task Refresh()
    {
        if (busy) return;
        busy = true;
        try
        {
            using var doc = JsonDocument.Parse(await http.GetStringAsync("api/ping"));
            var r = doc.RootElement;
            isMuted = r.TryGetProperty("muted", out var m) && m.GetBoolean();
            var mon = r.GetProperty("monitor");
            monitorOn = mon.GetProperty("on").GetBoolean();
            string lat = "";
            if (monitorOn && mon.GetProperty("status").TryGetProperty("latencyMs", out var l)) lat = $" · monitor {Math.Round(l.GetDouble())} ms";
            icon.Icon = isMuted ? (monitorOn ? mutedMon : muted) : (monitorOn ? liveMon : live);
            icon.Text = Trim($"{Title} · {(isMuted ? "mic muted" : "live")}{lat}");
            muteItem.Checked = isMuted;
            monitorItem.Checked = monitorOn;
        }
        catch { icon.Text = $"{Title} · starting..."; }
        finally { busy = false; }
    }

    async Task Post(string path, string json)
    {
        try { await http.PostAsync(path, new StringContent(json, Encoding.UTF8, "application/json")); } catch { }
        await Refresh();
    }

    static string Trim(string s) => s.Length > 63 ? s[..63] : s;  // tray tooltips are limited to 63 chars

    // A microphone glyph, drawn at runtime: no icon files to ship.
    static Icon Draw(Color c, bool dot)
    {
        using var bmp = new Bitmap(32, 32);
        using (var g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            using var pen = new Pen(c, 2.6f) { StartCap = LineCap.Round, EndCap = LineCap.Round };
            using var fill = new SolidBrush(c);
            using var body = new GraphicsPath();
            body.AddArc(11, 3, 10, 10, 180, 180);
            body.AddArc(11, 10, 10, 10, 0, 180);
            body.CloseFigure();
            g.FillPath(fill, body);
            g.DrawArc(pen, 6.5f, 6, 19, 19, 0, 180);
            g.DrawLine(pen, 16, 25, 16, 29);
            g.DrawLine(pen, 11, 29.5f, 21, 29.5f);
            if (dot)
            {
                using var green = new SolidBrush(Color.FromArgb(0, 234, 149));
                g.FillEllipse(green, 21, 0, 11, 11);
            }
        }
        return Icon.FromHandle(bmp.GetHicon());
    }

    public void Quit() => ui.Post(_ => { icon.Visible = false; Application.ExitThread(); }, null);

    public void Dispose() { icon.Dispose(); http.Dispose(); poll.Dispose(); watch.Dispose(); }
}

static class Win
{
    public const int SW_HIDE = 0, SW_SHOW = 5, SW_RESTORE = 9;
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    delegate bool EnumProc(IntPtr h, IntPtr p);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc f, IntPtr p);

    // The Chrome/Edge "app" window showing the page titled "Mic Control" (hidden ones included).
    public static IntPtr FindApp(string title)
    {
        IntPtr found = IntPtr.Zero;
        EnumWindows((h, _) =>
        {
            var t = new StringBuilder(256); GetWindowText(h, t, 256);
            if (t.ToString() != title) return true;
            var c = new StringBuilder(256); GetClassName(h, c, 256);
            if (!c.ToString().StartsWith("Chrome_WidgetWin")) return true;
            found = h;
            return false;
        }, IntPtr.Zero);
        return found;
    }
}
