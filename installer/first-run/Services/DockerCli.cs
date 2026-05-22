using System;
using System.Diagnostics;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace AIBox.FirstRun.Services;

/// <summary>
/// Thin wrapper around `docker` and `docker compose` so we don't repeat
/// argument-list plumbing in every consumer. All operations capture
/// stdout/stderr so callers can show them to the user on failure.
/// </summary>
public sealed class DockerCli
{
    public sealed class Result
    {
        public required int ExitCode { get; init; }
        public required string Stdout { get; init; }
        public required string Stderr { get; init; }
        public bool Ok => ExitCode == 0;
    }

    private readonly string _composeFile;
    private readonly FileLogger _log;
    private readonly Action<string>? _onLine;

    public DockerCli(string composeFile, FileLogger log, Action<string>? onLine = null)
    {
        _composeFile = composeFile;
        _log = log;
        _onLine = onLine;
    }

    public Task<Result> InfoAsync(CancellationToken ct) => RunDockerAsync(new[] { "info" }, ct);

    public Task<Result> ComposeAsync(string[] args, CancellationToken ct)
    {
        var full = new System.Collections.Generic.List<string> { "compose", "-f", _composeFile };
        full.AddRange(args);
        return RunDockerAsync(full.ToArray(), ct);
    }

    public async Task<bool> WaitForDaemonAsync(TimeSpan timeout, CancellationToken ct)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            ct.ThrowIfCancellationRequested();
            var r = await InfoAsync(ct).ConfigureAwait(false);
            if (r.Ok) return true;
            await Task.Delay(TimeSpan.FromSeconds(3), ct).ConfigureAwait(false);
        }
        return false;
    }

    private async Task<Result> RunDockerAsync(string[] args, CancellationToken ct)
    {
        var psi = new ProcessStartInfo("docker")
        {
            CreateNoWindow = true,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        foreach (var a in args) psi.ArgumentList.Add(a);

        using var proc = new Process { StartInfo = psi };
        var stdout = new StringBuilder();
        var stderr = new StringBuilder();
        proc.OutputDataReceived += (_, e) =>
        {
            if (e.Data == null) return;
            stdout.AppendLine(e.Data);
            _onLine?.Invoke(e.Data);
        };
        proc.ErrorDataReceived += (_, e) =>
        {
            if (e.Data == null) return;
            stderr.AppendLine(e.Data);
            _onLine?.Invoke(e.Data);
        };
        proc.Start();
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();
        try
        {
            await proc.WaitForExitAsync(ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            try { proc.Kill(true); } catch { /* swallow */ }
            throw;
        }
        _log.Info($"docker {string.Join(' ', args)} -> exit {proc.ExitCode}");
        return new Result { ExitCode = proc.ExitCode, Stdout = stdout.ToString(), Stderr = stderr.ToString() };
    }
}
