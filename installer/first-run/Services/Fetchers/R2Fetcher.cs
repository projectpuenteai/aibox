using System;
using System.IO;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using ICSharpCode.SharpZipLib.Tar;
using ZstdSharp;

namespace AIBox.FirstRun.Services.Fetchers;

/// <summary>
/// Fetches a single tar.zst shard from Cloudflare R2 (or any plain HTTPS
/// URL), verifies its SHA256, then extracts it into the manifest-specified
/// target directory. Each shard is independently extractable so a single
/// corrupt shard doesn't poison the full Chroma index.
/// </summary>
public sealed class R2Fetcher : IFetcher
{
    private readonly HttpClient _http;
    private readonly FileLogger _log;

    public string Source => "r2";

    public R2Fetcher(HttpClient http, FileLogger log)
    {
        _http = http;
        _log = log;
    }

    public async Task<FetchResult> FetchAsync(
        ManifestItem item,
        InstallContext ctx,
        IProgress<FetchProgress> progress,
        CancellationToken ct)
    {
        if (item is not R2Item r2)
            return new FetchResult { ItemId = item.Id, Success = false, IsRetryable = false,
                ErrorMessage = $"R2Fetcher received non-R2 item (source='{item.Source}')." };

        var targetPath = Path.Combine(ctx.DataRoot, r2.Target.Replace('/', Path.DirectorySeparatorChar));

        try
        {
            var bytesP = new Progress<long>(b => progress.Report(new FetchProgress
            {
                ItemId = item.Id,
                DisplayName = Path.GetFileName(r2.Target),
                BytesDownloaded = b,
                BytesTotal = r2.SizeBytes,
                Phase = "downloading",
            }));

            var dl = await HttpRangeDownloader.DownloadAsync(
                _http,
                new Uri(r2.Url),
                targetPath,
                r2.Sha256,
                r2.SizeBytes,
                bytesP,
                _log,
                ct).ConfigureAwait(false);

            if (!string.IsNullOrEmpty(r2.ExtractTo))
            {
                progress.Report(new FetchProgress
                {
                    ItemId = item.Id,
                    DisplayName = Path.GetFileName(r2.Target),
                    BytesDownloaded = dl.BytesWritten,
                    BytesTotal = dl.BytesWritten,
                    Phase = "extracting",
                });

                var extractRoot = Path.Combine(ctx.DataRoot,
                    r2.ExtractTo.Replace('/', Path.DirectorySeparatorChar));
                ExtractTarZst(targetPath, extractRoot, _log, ct);
            }

            progress.Report(new FetchProgress
            {
                ItemId = item.Id,
                DisplayName = Path.GetFileName(r2.Target),
                BytesDownloaded = dl.BytesWritten,
                BytesTotal = dl.BytesWritten,
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
            _log.Error($"R2 fetch failed for {item.Id}", ex);
            return new FetchResult
            {
                ItemId = item.Id,
                Success = false,
                ErrorMessage = ex.Message,
                Exception = ex,
                IsRetryable = ex is HttpRequestException or IOException,
            };
        }
    }

    /// <summary>
    /// Extract <paramref name="archivePath"/> (.tar.zst) into <paramref name="destDir"/>.
    /// Streaming decompress so we don't materialize the full tar in memory.
    /// Item 29: Validates that each tar entry's path does not escape destDir (defense in depth
    /// against malicious archives, even though the manifest is signed).
    /// </summary>
    public static void ExtractTarZst(string archivePath, string destDir, FileLogger log, CancellationToken ct)
    {
        Directory.CreateDirectory(destDir);
        var fullDestDir = Path.GetFullPath(destDir);
        using var fs = new FileStream(archivePath, FileMode.Open, FileAccess.Read, FileShare.Read);
        using var zs = new DecompressionStream(fs);
        using var tar = new TarInputStream(zs, System.Text.Encoding.UTF8);

        // Manually extract entries instead of trusting tar.ExtractContents().
        TarEntry entry;
        while ((entry = tar.GetNextEntry()) != null)
        {
            ct.ThrowIfCancellationRequested();

            // Validate that the computed target path doesn't escape destDir.
            var targetPath = Path.GetFullPath(Path.Combine(fullDestDir, entry.Name));
            if (!targetPath.StartsWith(fullDestDir + Path.DirectorySeparatorChar, StringComparison.Ordinal) &&
                targetPath != fullDestDir)
            {
                throw new InvalidDataException(
                    $"Tar archive entry '{entry.Name}' resolves outside destDir ({destDir}): {targetPath}");
            }

            if (entry.IsDirectory)
            {
                Directory.CreateDirectory(targetPath);
            }
            else
            {
                Directory.CreateDirectory(Path.GetDirectoryName(targetPath)!);
                using var dst = new FileStream(targetPath, FileMode.Create, FileAccess.Write, FileShare.None);
                tar.CopyEntryContents(dst);
            }
        }
        log.Info($"Extracted {Path.GetFileName(archivePath)} into {destDir}.");
    }
}
