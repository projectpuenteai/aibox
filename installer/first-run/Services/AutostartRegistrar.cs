using System;
using System.Diagnostics;
using System.IO;
using System.Security.Principal;
using System.Threading;
using System.Threading.Tasks;

namespace AIBox.FirstRun.Services;

/// <summary>
/// Runs the project's existing install_autostart.ps1 to register the
/// AIBox-Puente-Startup scheduled task. We do not re-invent the task
/// XML here — install_autostart.ps1 is already the source of truth.
/// </summary>
public sealed class AutostartRegistrar
{
    private readonly FileLogger _log;
    private readonly string _scriptPath;

    public AutostartRegistrar(InstallContext ctx, FileLogger log)
    {
        _log = log;
        _scriptPath = Path.Combine(
            ctx.InstallRoot, "aibox", "tools", "llama-runtime", "scripts", "install_autostart.ps1");
    }

    public async Task<bool> RegisterAsync(CancellationToken ct)
    {
        if (!IsRunningElevated())
        {
            _log.Warn("AutostartRegistrar.RegisterAsync requires elevation; skipping.");
            return false;
        }

        if (!File.Exists(_scriptPath))
        {
            _log.Error($"install_autostart.ps1 not found at {_scriptPath}");
            return false;
        }

        var psi = new ProcessStartInfo("powershell.exe")
        {
            CreateNoWindow = true,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        psi.ArgumentList.Add("-NoProfile");
        psi.ArgumentList.Add("-ExecutionPolicy"); psi.ArgumentList.Add("Bypass");
        psi.ArgumentList.Add("-File"); psi.ArgumentList.Add(_scriptPath);

        using var proc = new Process { StartInfo = psi };
        proc.Start();
        var stdout = await proc.StandardOutput.ReadToEndAsync(ct).ConfigureAwait(false);
        var stderr = await proc.StandardError.ReadToEndAsync(ct).ConfigureAwait(false);
        await proc.WaitForExitAsync(ct).ConfigureAwait(false);

        if (proc.ExitCode != 0)
        {
            _log.Error($"install_autostart.ps1 exited {proc.ExitCode}\nstdout: {stdout}\nstderr: {stderr}");
            return false;
        }
        _log.Info("Autostart task registered.");
        return true;
    }

    private static bool IsRunningElevated()
    {
        try
        {
            var identity = WindowsIdentity.GetCurrent();
            var principal = new WindowsPrincipal(identity);
            return principal.IsInRole(WindowsBuiltInRole.Administrator);
        }
        catch { return false; }
    }
}
