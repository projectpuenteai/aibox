using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace AIBox.FirstRun.Services.Fetchers;

/// <summary>
/// Drives Kolibri's own importchannel/importcontent commands via
/// `docker compose exec`. We do NOT download channel bytes directly —
/// Kolibri's importer handles checksums, resume, and delta updates,
/// which would be wasteful to reinvent. We just orchestrate.
///
/// Lifecycle within one engine run:
///   - First Kolibri item ensures `docker compose up -d kolibri` once.
///   - Each item runs importchannel + importcontent.
///   - On engine completion (Phase B end), DownloadPhase stops kolibri
///     so the stack returns to fully-stopped before Phase C.
/// </summary>
public sealed class KolibriFetcher : IFetcher
{
    private static readonly SemaphoreSlim StartLock = new(1, 1);
    private static bool _kolibriUp;
    private readonly FileLogger _log;
    private readonly string _composeFile;

    public string Source => "kolibri_channel";

    public KolibriFetcher(FileLogger log, InstallContext ctx)
    {
        _log = log;
        _composeFile = ctx.ComposeFile;
    }

    public async Task<FetchResult> FetchAsync(
        ManifestItem item,
        InstallContext ctx,
        IProgress<FetchProgress> progress,
        CancellationToken ct)
    {
        if (item is not KolibriChannelItem kc)
            return new FetchResult { ItemId = item.Id, Success = false, IsRetryable = false,
                ErrorMessage = $"KolibriFetcher received non-Kolibri item (source='{item.Source}')." };
        if (string.IsNullOrWhiteSpace(kc.ChannelId))
            return new FetchResult { ItemId = item.Id, Success = false, IsRetryable = false,
                ErrorMessage = "Kolibri item missing channel_id." };

        try
        {
            await EnsureKolibriUpAsync(ct).ConfigureAwait(false);

            progress.Report(new FetchProgress
            {
                ItemId = item.Id,
                DisplayName = $"Kolibri channel {Shorten(kc.ChannelId)}",
                Phase = "downloading",
                Detail = "importchannel",
            });
            await RunImportAsync("importchannel", kc.ChannelId, item.Id, progress, ct).ConfigureAwait(false);

            progress.Report(new FetchProgress
            {
                ItemId = item.Id,
                DisplayName = $"Kolibri channel {Shorten(kc.ChannelId)}",
                Phase = "downloading",
                Detail = "importcontent",
            });
            await RunImportAsync("importcontent", kc.ChannelId, item.Id, progress, ct).ConfigureAwait(false);

            progress.Report(new FetchProgress
            {
                ItemId = item.Id,
                DisplayName = $"Kolibri channel {Shorten(kc.ChannelId)}",
                Phase = "done",
            });
            return new FetchResult { ItemId = item.Id, Success = true };
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception ex)
        {
            _log.Error($"Kolibri import failed for channel {kc.ChannelId}", ex);
            return new FetchResult
            {
                ItemId = item.Id, Success = false, IsRetryable = true,
                ErrorMessage = ex.Message, Exception = ex,
            };
        }
    }

    private async Task EnsureKolibriUpAsync(CancellationToken ct)
    {
        await StartLock.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            if (_kolibriUp) return;

            // Item 40: Check if kolibri is already running via docker compose ps.
            // If the First Run app is restarted, the static flag resets; detect
            // live container to avoid redundant `docker compose up` calls.
            var (psCode, psStdout, _) = await RunDockerComposeAsync(
                new[] { "ps", "-q", "kolibri" }, ct).ConfigureAwait(false);
            if (psCode == 0 && !string.IsNullOrWhiteSpace(psStdout))
            {
                _log.Info("Kolibri container is already running.");
                _kolibriUp = true;
                return;
            }

            _log.Info("Ensuring kolibri container is up via docker compose.");
            var (code, _, stderr) = await RunDockerComposeAsync(
                new[] { "up", "-d", "kolibri" }, ct).ConfigureAwait(false);
            if (code != 0)
                throw new IOException($"docker compose up -d kolibri failed (exit {code}): {stderr}");
            _kolibriUp = true;
        }
        finally
        {
            StartLock.Release();
        }
    }

    public async Task StopKolibriAsync(CancellationToken ct)
    {
        await StartLock.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            if (!_kolibriUp) return;
            _log.Info("Stopping kolibri container.");
            await RunDockerComposeAsync(new[] { "stop", "kolibri" }, ct).ConfigureAwait(false);
            _kolibriUp = false;
        }
        finally
        {
            StartLock.Release();
        }
    }

    private async Task RunImportAsync(string command, string channelId, string itemId, IProgress<FetchProgress> progress, CancellationToken ct)
    {
        var args = new[]
        {
            "exec", "-T", "kolibri",
            "kolibri", "manage", command, "network", channelId,
        };

        var psi = new ProcessStartInfo("docker", BuildArgList(args))
        {
            CreateNoWindow = true,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        psi.ArgumentList.Clear();
        psi.ArgumentList.Add("compose");
        psi.ArgumentList.Add("-f"); psi.ArgumentList.Add(_composeFile);
        foreach (var a in args) psi.ArgumentList.Add(a);

        using var proc = new Process { StartInfo = psi };
        var stderrBuf = new StringBuilder();
        proc.OutputDataReceived += (_, e) =>
        {
            if (string.IsNullOrEmpty(e.Data)) return;
            TryReportProgress(e.Data!, itemId, progress);
        };
        proc.ErrorDataReceived += (_, e) =>
        {
            if (string.IsNullOrEmpty(e.Data)) return;
            stderrBuf.AppendLine(e.Data);
            // Kolibri sometimes writes progress to stderr; try parsing.
            TryReportProgress(e.Data!, itemId, progress);
        };

        proc.Start();
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();
        await proc.WaitForExitAsync(ct).ConfigureAwait(false);

        if (proc.ExitCode != 0)
            throw new IOException($"kolibri manage {command} exited {proc.ExitCode}: {stderrBuf.ToString().Trim()}");
    }

    private static void TryReportProgress(string line, string itemId, IProgress<FetchProgress> progress)
    {
        var trimmed = line.Trim();
        if (!trimmed.StartsWith("{") || !trimmed.EndsWith("}")) return;
        try
        {
            var obj = JsonNode.Parse(trimmed)?.AsObject();
            if (obj == null) return;
            var pct = obj["percent_complete"]?.GetValue<double>() ?? -1;
            var msg = obj["message"]?.GetValue<string>() ?? "";
            if (pct < 0) return;
            progress.Report(new FetchProgress
            {
                ItemId = itemId,
                DisplayName = "Kolibri import",
                BytesDownloaded = (long)pct,
                BytesTotal = 100,
                Phase = "downloading",
                Detail = msg,
            });
        }
        catch { /* not all stdout lines are JSON; ignore */ }
    }

    private async Task<(int code, string stdout, string stderr)> RunDockerComposeAsync(string[] args, CancellationToken ct)
    {
        var psi = new ProcessStartInfo("docker")
        {
            CreateNoWindow = true,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        psi.ArgumentList.Add("compose");
        psi.ArgumentList.Add("-f"); psi.ArgumentList.Add(_composeFile);
        foreach (var a in args) psi.ArgumentList.Add(a);

        using var proc = new Process { StartInfo = psi };
        var stdout = new StringBuilder();
        var stderr = new StringBuilder();
        proc.OutputDataReceived += (_, e) => { if (e.Data != null) stdout.AppendLine(e.Data); };
        proc.ErrorDataReceived += (_, e) => { if (e.Data != null) stderr.AppendLine(e.Data); };
        proc.Start();
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();
        await proc.WaitForExitAsync(ct).ConfigureAwait(false);
        return (proc.ExitCode, stdout.ToString(), stderr.ToString());
    }

    private static string Shorten(string id) => id.Length > 8 ? id[..8] : id;

    private static string BuildArgList(string[] args) => string.Join(' ', args);
}
