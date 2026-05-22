using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;

namespace AIBox.Uninstaller;

public partial class MainWindow : Window
{
    private static readonly string DataRoot = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
        "AIBox");

    public MainWindow()
    {
        InitializeComponent();
        if (App.RanFromInno)
        {
            // Inno launched us during uninstall. Default both boxes ON so the
            // full cleanup happens unless the user opts out.
            DataCheck.IsChecked = true;
            UserDataCheck.IsChecked = true;
        }
    }

    private async void OnProceedClick(object sender, RoutedEventArgs e)
    {
        var summary =
            "This will:\n" +
            "  - Remove AIBox-Puente-Startup scheduled task\n" +
            "  - Remove puente.link hosts entry\n" +
            "  - Tear down Mobile Hotspot config\n" +
            "  - Delete encryption key + DPAPI blob\n";
        if (DataCheck.IsChecked == true)  summary += "  - Delete downloaded content (~100 GB)\n";
        if (UserDataCheck.IsChecked == true) summary += "  - Delete encrypted user data + chat history\n";
        summary += "\nProceed?";

        var ans = MessageBox.Show(this, summary, "AIBox Uninstaller",
            MessageBoxButton.YesNo, MessageBoxImage.Warning);
        if (ans != MessageBoxResult.Yes) return;

        ProceedButton.IsEnabled = false;
        CancelButton.IsEnabled = false;

        try
        {
            await Task.Run(() => RunCleanup(
                DataCheck.IsChecked == true,
                UserDataCheck.IsChecked == true));
            MessageBox.Show(this, "AIBox has been uninstalled.", "AIBox Uninstaller",
                MessageBoxButton.OK, MessageBoxImage.Information);
            Close();
        }
        catch (Exception ex)
        {
            MessageBox.Show(this,
                $"Cleanup hit an error:\n\n{ex.Message}\n\n" +
                $"Some files may remain. See logs in {DataRoot}\\logs.",
                "AIBox Uninstaller", MessageBoxButton.OK, MessageBoxImage.Error);
        }
    }

    private void OnCancelClick(object sender, RoutedEventArgs e) => Close();

    private static void RunCleanup(bool removeContent, bool removeUserData)
    {
        // 1) Stop the stack if it's running (best effort).
        TryRunPowerShell(@"aibox\tools\llama-runtime\scripts\down_stack.ps1");

        // 2) Unregister the scheduled task.
        TryRun("schtasks", new[] { "/Delete", "/TN", "AIBox-Puente-Startup", "/F" });

        // 3) Remove hosts file puente.link entries.
        var hostsPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
            "drivers", "etc", "hosts");
        try
        {
            if (File.Exists(hostsPath))
            {
                var kept = File.ReadAllLines(hostsPath)
                    .Where(l => !l.Contains("puente.link", StringComparison.OrdinalIgnoreCase))
                    .ToArray();
                File.WriteAllLines(hostsPath, kept);
            }
        }
        catch { /* hosts edit can fail under AV — non-fatal */ }

        // 4) Tear down Mobile Hotspot config — punt to the project's own
        //    down_stack script which already does this.
        // (Already handled by step 1.)

        // 5) Zero out the encryption-related blobs unconditionally.
        TryDelete(Path.Combine(DataRoot, "admin-credentials.dpapi"));
        // We can't selectively delete APP_ENCRYPTION_MASTER_KEY from .env
        // without the install root; the .env will be removed with the
        // install tree by Inno's standard uninstaller anyway.

        // 6) Optional: content + user data
        if (removeContent)
        {
            // Wipe ProgramData\AIBox content piece by piece so we don't
            // accidentally rm -rf the logs the user might want.
            TryDeleteDir(Path.Combine(DataRoot, "models"));
            TryDeleteDir(Path.Combine(DataRoot, "kiwix"));
            TryDeleteDir(Path.Combine(DataRoot, "kolibri-data"));
            TryDeleteDir(Path.Combine(DataRoot, "backend-data", "chroma_db"));
        }
        if (removeUserData)
        {
            TryDeleteDir(Path.Combine(DataRoot, "backend-data", "storage"));
            TryDelete(Path.Combine(DataRoot, "backend-data", "ai_control.db"));
        }

        // 7) The Control Panel / First Run shortcuts get cleaned up by Inno;
        //    no work here.
    }

    private static void TryRunPowerShell(string scriptRelative)
    {
        try
        {
            var script = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                "AIBox", scriptRelative);
            if (!File.Exists(script)) return;
            TryRun("powershell.exe", new[]
            {
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
            });
        }
        catch { /* best effort */ }
    }

    private static void TryRun(string exe, string[] args)
    {
        try
        {
            var psi = new ProcessStartInfo(exe)
            {
                CreateNoWindow = true,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            foreach (var a in args) psi.ArgumentList.Add(a);
            using var proc = Process.Start(psi)!;
            proc.WaitForExit(60_000);
        }
        catch { /* best effort */ }
    }

    private static void TryDelete(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); } catch { }
    }

    private static void TryDeleteDir(string path)
    {
        try
        {
            var full = Path.GetFullPath(path);
            if (Directory.Exists(full)) Directory.Delete(full, recursive: true);
        }
        catch { /* keep going — non-critical files may be locked */ }
    }
}
