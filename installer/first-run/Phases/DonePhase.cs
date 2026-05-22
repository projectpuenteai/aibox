using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Controls;
using System.Windows.Documents;
using AIBox.FirstRun.Services;

namespace AIBox.FirstRun.Phases;

/// <summary>
/// Terminal state: Phase C is complete. We surface a "Open Control
/// Panel" link, delete the DPAPI admin password blob on first display,
/// and otherwise stay out of the way. Re-opening First Run after this
/// point is harmless — we just land here again.
/// </summary>
public sealed class DonePhase : UserControl, IPhase
{
#pragma warning disable CS0067 // IPhase contract requires the event; terminal phase has nothing to report.
    public event EventHandler<string>? StatusChanged;
#pragma warning restore CS0067

    public DonePhase(InstallContext ctx)
    {
        var panel = new StackPanel();
        panel.Children.Add(new TextBlock
        {
            Text = "AIBox is installed.",
            FontSize = 18,
            FontWeight = System.Windows.FontWeights.SemiBold,
            Margin = new System.Windows.Thickness(0, 0, 0, 8),
        });
        panel.Children.Add(new TextBlock
        {
            Text = "Open AIBox Control Panel from your desktop to start the stack.",
        });

        var openBtn = new System.Windows.Controls.Button
        {
            Content = "Open Control Panel",
            Margin = new System.Windows.Thickness(0, 16, 0, 0),
            HorizontalAlignment = System.Windows.HorizontalAlignment.Left,
        };
        openBtn.Click += (_, _) => LaunchControlPanel(ctx);
        panel.Children.Add(openBtn);

        Content = panel;

        // Best-effort: drop the DPAPI admin password blob now that the user
        // has reached this screen. Anyone reaching DonePhase has either just
        // finished install (and was shown the summary) or has launched a
        // second time (in which case the secret is no longer needed).
        EnvWriter.DeleteDpapiAdminBlob(Path.Combine(ctx.DataRoot, "admin-credentials.dpapi"));
    }

    public Task<PhaseResult> RunAsync(CancellationToken ct) =>
        Task.FromResult(PhaseResult.Done());

    private static void LaunchControlPanel(InstallContext ctx)
    {
        var script = Path.Combine(ctx.InstallRoot, "aibox", "tools", "llama-runtime", "scripts", "aibox_control_ui.ps1");
        if (!File.Exists(script)) return;
        var psi = new ProcessStartInfo("powershell.exe")
        {
            UseShellExecute = true,
            Verb = "runas",
        };
        psi.ArgumentList.Add("-NoProfile");
        psi.ArgumentList.Add("-ExecutionPolicy"); psi.ArgumentList.Add("Bypass");
        psi.ArgumentList.Add("-File"); psi.ArgumentList.Add(script);
        try { Process.Start(psi); } catch { /* user cancelled UAC */ }
    }
}
