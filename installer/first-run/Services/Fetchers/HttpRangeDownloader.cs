using System;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Threading;
using System.Threading.Tasks;

namespace AIBox.FirstRun.Services.Fetchers;

/// <summary>
/// Generic resumable downloader used by the R2, HuggingFace, and Kiwix
/// fetchers. Writes to &lt;target&gt;.part with HTTP Range, then atomically
/// renames after SHA256 verification. Idempotent: re-running on a fully
/// verified target is a no-op.
/// </summary>
public static class HttpRangeDownloader
{
    public sealed class Result
    {
        public required long BytesWritten { get; init; }
        public required string ActualSha256 { get; init; }
    }

    /// <summary>Download <paramref name="url"/> to <paramref name="targetPath"/> with resume + SHA verify.</summary>
    /// <param name="expectedSha256">Lowercase hex. Pass null/empty to skip verification (caller assumes responsibility).</param>
    /// <param name="expectedSize">Total expected size in bytes; pass 0 if unknown.</param>
    public static async Task<Result> DownloadAsync(
        HttpClient http,
        Uri url,
        string targetPath,
        string? expectedSha256,
        long expectedSize,
        IProgress<long> bytesProgress,
        FileLogger log,
        CancellationToken ct,
        HttpRequestHeaders? extraHeaders = null)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(targetPath)!);
        var partPath = targetPath + ".part";

        if (File.Exists(targetPath) && !string.IsNullOrEmpty(expectedSha256))
        {
            var existingSha = await ComputeFileSha256Async(targetPath, ct).ConfigureAwait(false);
            if (string.Equals(existingSha, expectedSha256, StringComparison.OrdinalIgnoreCase))
            {
                log.Info($"Target already present + verified: {targetPath}");
                var len = new FileInfo(targetPath).Length;
                bytesProgress.Report(len);
                return new Result { BytesWritten = len, ActualSha256 = existingSha };
            }
            log.Warn($"Target exists but SHA mismatch — re-downloading: {targetPath}");
            File.Delete(targetPath);
        }

        long resumeFrom = File.Exists(partPath) ? new FileInfo(partPath).Length : 0;
        if (resumeFrom > 0)
            log.Info($"Resuming {url} from byte {resumeFrom} into {partPath}.");
        else
            log.Info($"Starting download {url} -> {partPath}.");

        // Attempt 1: range-resume request (or fresh request if resumeFrom == 0).
        // On 416, delete .part and retry from byte 0 once.
        for (int attempt = 0; attempt < 2; attempt++)
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, url);
            if (resumeFrom > 0)
                req.Headers.Range = new RangeHeaderValue(resumeFrom, null);
            if (extraHeaders != null)
            {
                foreach (var h in extraHeaders)
                    req.Headers.TryAddWithoutValidation(h.Key, h.Value);
            }

            using var resp = await http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct).ConfigureAwait(false);

            if (resp.StatusCode == HttpStatusCode.RequestedRangeNotSatisfiable)
            {
                if (resumeFrom > 0 && attempt == 0)
                {
                    log.Warn("Server reported range not satisfiable; deleting .part and retrying from byte 0.");
                    if (File.Exists(partPath)) File.Delete(partPath);
                    resumeFrom = 0;
                    continue;  // re-issue request with no Range header
                }
                // resumeFrom is already 0 (or we've already retried once) — verify whatever's on disk.
                log.Warn("Server reported range not satisfiable at byte 0; verifying existing .part.");
                break;
            }
            if (resumeFrom > 0 && resp.StatusCode != HttpStatusCode.PartialContent)
            {
                log.Warn($"Server ignored Range header (status {resp.StatusCode}); restarting from byte 0.");
                if (File.Exists(partPath)) File.Delete(partPath);
                resumeFrom = 0;
                continue;
            }
            resp.EnsureSuccessStatusCode();

            long total = expectedSize;
            if (total <= 0 && resp.Content.Headers.ContentLength is long advertised)
            {
                // Defend against overflow on malicious/broken Content-Length.
                total = (advertised > long.MaxValue - resumeFrom) ? long.MaxValue : resumeFrom + advertised;
                if (expectedSize > 0 && advertised != expectedSize - resumeFrom)
                    log.Warn($"Content-Length {advertised} disagrees with expected remaining " +
                             $"{expectedSize - resumeFrom} bytes for {targetPath}.");
            }

            using (var src = await resp.Content.ReadAsStreamAsync(ct).ConfigureAwait(false))
            using (var dst = new FileStream(partPath, FileMode.Append, FileAccess.Write, FileShare.Read))
            {
                var buffer = new byte[1024 * 256];
                long written = resumeFrom;
                int read;
                while ((read = await src.ReadAsync(buffer.AsMemory(0, buffer.Length), ct).ConfigureAwait(false)) > 0)
                {
                    await dst.WriteAsync(buffer.AsMemory(0, read), ct).ConfigureAwait(false);
                    written += read;
                    bytesProgress.Report(written);
                }
                await dst.FlushAsync(ct).ConfigureAwait(false);
                dst.Flush(true);
            }
            break;  // successful pass through the body; verify below
        }

        var actualSha = await ComputeFileSha256Async(partPath, ct).ConfigureAwait(false);
        if (!string.IsNullOrEmpty(expectedSha256) &&
            !string.Equals(actualSha, expectedSha256, StringComparison.OrdinalIgnoreCase))
        {
            log.Error($"SHA256 mismatch for {targetPath}: expected={expectedSha256}, got={actualSha}");
            File.Delete(partPath);
            throw new IOException(
                $"Downloaded file failed checksum verification.\n  expected: {expectedSha256}\n  actual:   {actualSha}");
        }

        if (File.Exists(targetPath)) File.Delete(targetPath);
        File.Move(partPath, targetPath);
        var finalLen = new FileInfo(targetPath).Length;
        log.Info($"Download verified: {targetPath} ({finalLen:N0} bytes).");
        return new Result { BytesWritten = finalLen, ActualSha256 = actualSha };
    }

    public static async Task<string> ComputeFileSha256Async(string path, CancellationToken ct)
    {
        using var sha = SHA256.Create();
        using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read,
            bufferSize: 1024 * 256, useAsync: true);
        var buf = new byte[1024 * 256];
        int read;
        while ((read = await fs.ReadAsync(buf.AsMemory(0, buf.Length), ct).ConfigureAwait(false)) > 0)
            sha.TransformBlock(buf, 0, read, null, 0);
        sha.TransformFinalBlock(Array.Empty<byte>(), 0, 0);
        return Convert.ToHexString(sha.Hash!).ToLowerInvariant();
    }
}
