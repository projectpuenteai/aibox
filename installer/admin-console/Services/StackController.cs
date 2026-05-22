using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Threading.Tasks;

namespace AIBox.AdminConsole.Services;

/// <summary>
/// Shells out to up_stack.ps1 / down_stack.ps1 via powershell.exe and streams
/// each output line both to an optional UI callback and to the admin-console
/// log file. Pattern adapted from aibox/installer/first-run/Services/DockerCli.cs.
///
/// The line callback receives (streamTag, line) where streamTag is "out" or
/// "err"; UI consumers can colour-code error lines without re-parsing strings.
/// </summary>
public static class StackController
{
    public sealed record RunResult(int ExitCode, string? ErrorMessage)
    {
        public bool Ok => ExitCode == 0 && ErrorMessage is null;
    }

    public static Task<RunResult> StartStackAsync(Action<string, string>? onLine, CancellationToken ct) =>
        RunPowerShellAsync(PathResolver.UpScript(), onLine, ct);

    public static Task<RunResult> StopStackAsync(Action<string, string>? onLine, CancellationToken ct) =>
        RunPowerShellAsync(PathResolver.DownScript(), onLine, ct);

    private static async Task<RunResult> RunPowerShellAsync(string scriptPath, Action<string, string>? onLine, CancellationToken ct)
    {
        if (!File.Exists(scriptPath))
        {
            var err = $"script not found: {scriptPath}";
            onLine?.Invoke("err", err);
            return new RunResult(-1, err);
        }

        var psi = new ProcessStartInfo("powershell.exe")
        {
            CreateNoWindow = true,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        psi.ArgumentList.Add("-NoProfile");
        psi.ArgumentList.Add("-ExecutionPolicy");
        psi.ArgumentList.Add("Bypass");
        psi.ArgumentList.Add("-File");
        psi.ArgumentList.Add(scriptPath);

        using var proc = new Process { StartInfo = psi };

        void HandleLine(string tag, string? data)
        {
            if (string.IsNullOrEmpty(data)) return;
            FileLog.Append(App.LogFile, $"[{tag}] {data}");
            onLine?.Invoke(tag, data);
        }

        proc.OutputDataReceived += (_, e) => HandleLine("out", e.Data);
        proc.ErrorDataReceived  += (_, e) => HandleLine("err", e.Data);

        var header = $"----- run: {Path.GetFileName(scriptPath)} -----";
        FileLog.Append(App.LogFile, header);
        onLine?.Invoke("sys", header);

        try
        {
            proc.Start();
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();
            await proc.WaitForExitAsync(ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            try { proc.Kill(true); } catch { /* best effort */ }
            return new RunResult(-1, "cancelled");
        }
        catch (Exception ex)
        {
            var msg = $"----- run threw: {ex.Message} -----";
            FileLog.Append(App.LogFile, msg);
            onLine?.Invoke("err", msg);
            return new RunResult(-1, ex.Message);
        }

        var footer = $"----- exit {proc.ExitCode}: {Path.GetFileName(scriptPath)} -----";
        FileLog.Append(App.LogFile, footer);
        onLine?.Invoke("sys", footer);
        return new RunResult(proc.ExitCode, proc.ExitCode == 0 ? null : $"exit code {proc.ExitCode}");
    }
}
