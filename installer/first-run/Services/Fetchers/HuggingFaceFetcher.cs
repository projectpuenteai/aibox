using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace AIBox.FirstRun.Services.Fetchers;

/// <summary>
/// Fetches single files or curated subtrees from huggingface.co.
/// Single-file items resolve to /resolve/{sha}/{path} and use range-resume.
/// Multi-file items enumerate the repo tree at the pinned commit, filter
/// by include globs, fetch each file, then verify the sorted (path,sha256)
/// list against the manifest's sha256_manifest.
/// </summary>
public sealed class HuggingFaceFetcher : IFetcher
{
    private readonly HttpClient _http;
    private readonly FileLogger _log;

    public string Source => "huggingface";

    public HuggingFaceFetcher(HttpClient http, FileLogger log)
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
        if (item is not HuggingFaceItem hf)
            return new FetchResult { ItemId = item.Id, Success = false, IsRetryable = false,
                ErrorMessage = $"HuggingFaceFetcher received non-HF item (source='{item.Source}')." };

        try
        {
            if (hf.IsMultiFile)
                await FetchMultiAsync(hf, ctx, progress, ct).ConfigureAwait(false);
            else
                await FetchSingleAsync(hf, ctx, progress, ct).ConfigureAwait(false);

            return new FetchResult { ItemId = item.Id, Success = true };
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (HttpRequestException ex) when (ex.StatusCode == HttpStatusCode.TooManyRequests)
        {
            // Item 28: For HF rate-limits, signal the engine to use 30s/2m/5m backoffs
            // (much longer than default) per installerplan §6.
            return new FetchResult
            {
                ItemId = item.Id, Success = false, IsRetryable = true,
                ErrorMessage = "Hugging Face is rate-limiting this connection. Set AIBOX_HF_TOKEN (advanced panel) with a free HF token to continue.",
                Exception = ex,
                RetryAfterHint = TimeSpan.FromSeconds(30), // Engine will use HF-specific backoff array.
            };
        }
        catch (HttpRequestException ex) when (ex.StatusCode == HttpStatusCode.NotFound)
        {
            return new FetchResult
            {
                ItemId = item.Id, Success = false, IsRetryable = false,
                ErrorMessage = $"Hugging Face returned 404 for {hf.Repo}@{hf.Revision}. Manifest points at a revision that no longer exists; download a newer setup.exe / manifest.",
                Exception = ex,
            };
        }
        catch (Exception ex)
        {
            _log.Error($"HF fetch failed for {item.Id}", ex);
            return new FetchResult
            {
                ItemId = item.Id, Success = false, IsRetryable = true,
                ErrorMessage = ex.Message, Exception = ex,
            };
        }
    }

    private async Task FetchSingleAsync(HuggingFaceItem hf, InstallContext ctx, IProgress<FetchProgress> progress, CancellationToken ct)
    {
        if (string.IsNullOrEmpty(hf.PathInRepo) || string.IsNullOrEmpty(hf.Target))
            throw new InvalidDataException($"Single-file HF item {hf.Id} missing path_in_repo or target.");

        var url = ResolveUrl(hf.Repo, hf.Revision, hf.PathInRepo);
        var target = Path.Combine(ctx.DataRoot, hf.Target.Replace('/', Path.DirectorySeparatorChar));

        var bytesP = new Progress<long>(b => progress.Report(new FetchProgress
        {
            ItemId = hf.Id,
            DisplayName = $"{hf.Repo}/{hf.PathInRepo}",
            BytesDownloaded = b,
            BytesTotal = hf.SizeBytes,
            Phase = "downloading",
        }));

        // Build auth headers inline.
        var authReq = new HttpRequestMessage();
        ApplyAuthHeaders(authReq);
        var extraHeaders = authReq.Headers;

        await HttpRangeDownloader.DownloadAsync(
            _http, url, target,
            string.IsNullOrEmpty(hf.Sha256) ? null : hf.Sha256,
            hf.SizeBytes, bytesP, _log, ct,
            extraHeaders: extraHeaders).ConfigureAwait(false);

        progress.Report(new FetchProgress
        {
            ItemId = hf.Id,
            DisplayName = $"{hf.Repo}/{hf.PathInRepo}",
            BytesDownloaded = hf.SizeBytes,
            BytesTotal = hf.SizeBytes,
            Phase = "done",
        });
    }

    private async Task FetchMultiAsync(HuggingFaceItem hf, InstallContext ctx, IProgress<FetchProgress> progress, CancellationToken ct)
    {
        if (string.IsNullOrEmpty(hf.TargetDir))
            throw new InvalidDataException($"Multi-file HF item {hf.Id} missing target_dir.");

        var targetDir = Path.Combine(ctx.DataRoot, hf.TargetDir.Replace('/', Path.DirectorySeparatorChar));
        Directory.CreateDirectory(targetDir);

        // 1) Resolve the file list. If the manifest's `files` list is populated
        // (built by CI), prefer it — it carries authoritative per-file SHA256s.
        // Otherwise hit the HF tree API and assemble it ourselves.
        IReadOnlyList<HfFileEntry> files;
        if (hf.Files is { Count: > 0 })
        {
            files = hf.Files;
        }
        else
        {
            files = await ListRepoTreeAsync(hf.Repo, hf.Revision, hf.Include ?? new List<string>(), ct).ConfigureAwait(false);
        }

        // 2) Fetch each file. We're already inside the engine's per-source
        // semaphore (cap=2 for HF), so don't add another layer of parallelism
        // here — sequential within an item, parallel across items.
        long totalBytes = files.Sum(f => f.SizeBytes);
        long doneBytes = 0;
        var sha256Lines = new List<string>();

        // Prepare auth headers once for all files in this multi-item.
        var authReq = new HttpRequestMessage();
        ApplyAuthHeaders(authReq);
        var extraHeaders = authReq.Headers;

        foreach (var f in files.OrderBy(f => f.Path, StringComparer.Ordinal))
        {
            ct.ThrowIfCancellationRequested();
            var url = ResolveUrl(hf.Repo, hf.Revision, f.Path);
            var dest = Path.Combine(targetDir, f.Path.Replace('/', Path.DirectorySeparatorChar));

            long fileStart = doneBytes;
            var bytesP = new Progress<long>(b => progress.Report(new FetchProgress
            {
                ItemId = hf.Id,
                DisplayName = $"{hf.Repo}/{f.Path}",
                BytesDownloaded = fileStart + b,
                BytesTotal = totalBytes > 0 ? totalBytes : hf.SizeBytesTotal,
                Phase = "downloading",
            }));

            var dl = await HttpRangeDownloader.DownloadAsync(
                _http, url, dest,
                string.IsNullOrEmpty(f.Sha256) ? null : f.Sha256,
                f.SizeBytes, bytesP, _log, ct,
                extraHeaders: extraHeaders).ConfigureAwait(false);

            doneBytes += dl.BytesWritten;
            sha256Lines.Add($"{dl.ActualSha256}  {f.Path}");
        }

        // 3) Verify the bag-of-files manifest. The expected hash is the
        // SHA256 of "{sha}  {path}\n" lines sorted by path, ASCII LF only.
        if (!string.IsNullOrEmpty(hf.Sha256Manifest))
        {
            var joined = string.Join("\n", sha256Lines.OrderBy(l => l, StringComparer.Ordinal)) + "\n";
            var actual = System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(joined));
            var actualHex = Convert.ToHexString(actual).ToLowerInvariant();
            if (!string.Equals(actualHex, hf.Sha256Manifest, StringComparison.OrdinalIgnoreCase))
            {
                throw new IOException(
                    $"HF subtree manifest hash mismatch for {hf.Id}.\n  expected: {hf.Sha256Manifest}\n  actual:   {actualHex}");
            }
            _log.Info($"HF subtree manifest verified for {hf.Id}.");
        }

        progress.Report(new FetchProgress
        {
            ItemId = hf.Id,
            DisplayName = hf.Repo,
            BytesDownloaded = doneBytes,
            BytesTotal = totalBytes,
            Phase = "done",
        });
    }

    private async Task<List<HfFileEntry>> ListRepoTreeAsync(
        string repo, string revision, List<string> includeGlobs, CancellationToken ct)
    {
        var globber = new GlobMatcher(includeGlobs);
        var files = new List<HfFileEntry>();

        // HF caps tree responses at 1000 entries and exposes a Link header with
        // rel="next" for pagination. Follow it until exhausted. Safety cap of
        // 100 pages prevents infinite loops on a broken upstream.
        var nextUrl = $"https://huggingface.co/api/models/{repo}/tree/{revision}?recursive=true";
        int page = 0;
        while (nextUrl is not null && page < 100)
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, new Uri(nextUrl));
            ApplyAuthHeaders(req);

            using var resp = await _http.SendAsync(req, ct).ConfigureAwait(false);
            resp.EnsureSuccessStatusCode();
            var bytes = await resp.Content.ReadAsByteArrayAsync(ct).ConfigureAwait(false);
            var root = JsonNode.Parse(bytes)?.AsArray()
                       ?? throw new InvalidDataException("HF tree API returned non-array.");

            foreach (var node in root)
            {
                if (node is not JsonObject o) continue;
                var type = o["type"]?.GetValue<string>();
                if (type != "file") continue;
                var path = o["path"]?.GetValue<string>() ?? "";
                if (!globber.Match(path)) continue;
                files.Add(new HfFileEntry
                {
                    Path = path,
                    SizeBytes = o["size"]?.GetValue<long>() ?? 0,
                    Sha256 = "",
                });
            }

            nextUrl = ParseLinkHeaderNext(resp);
            page++;
        }
        if (page == 100)
            throw new InvalidDataException(
                $"HF tree API for {repo}@{revision} returned more than 100 pages — refusing.");
        return files;
    }

    private static string? ParseLinkHeaderNext(HttpResponseMessage resp)
    {
        if (!resp.Headers.TryGetValues("Link", out var values)) return null;
        foreach (var raw in values)
        {
            // Format: <url>; rel="next", <url2>; rel="prev"
            foreach (var part in raw.Split(','))
            {
                var trimmed = part.Trim();
                if (!trimmed.Contains("rel=\"next\"")) continue;
                var lt = trimmed.IndexOf('<');
                var gt = trimmed.IndexOf('>');
                if (lt >= 0 && gt > lt) return trimmed.Substring(lt + 1, gt - lt - 1);
            }
        }
        return null;
    }

    private static Uri ResolveUrl(string repo, string revision, string path) =>
        new($"https://huggingface.co/{repo}/resolve/{revision}/{path}");

    /// <summary>
    /// Item 13: Apply HF auth header directly to a request message.
    /// Previous implementation returned HttpRequestHeaders from a transient message,
    /// which was disposed before use (subtle lifecycle bug). Instead, apply the
    /// header directly here and inline auth setup in call sites.
    /// </summary>
    private static void ApplyAuthHeaders(HttpRequestMessage req)
    {
        var token = Environment.GetEnvironmentVariable(BuildConstants.HfTokenEnvVar);
        if (!string.IsNullOrEmpty(token))
            req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
    }
}

/// <summary>
/// Tiny include-glob matcher: supports '*' (anything but '/'), '**'
/// (anything including '/'), and literal characters. Sufficient for the
/// HF include patterns we use ("*.json", "**/*.safetensors", etc).
/// </summary>
internal sealed class GlobMatcher
{
    private readonly List<System.Text.RegularExpressions.Regex> _patterns;

    public GlobMatcher(IEnumerable<string> globs)
    {
        _patterns = globs.Select(GlobToRegex).ToList();
    }

    public bool Match(string path)
    {
        if (_patterns.Count == 0) return true;
        foreach (var r in _patterns)
            if (r.IsMatch(path)) return true;
        return false;
    }

    private static System.Text.RegularExpressions.Regex GlobToRegex(string glob)
    {
        var sb = new System.Text.StringBuilder("^");
        for (int i = 0; i < glob.Length; i++)
        {
            var c = glob[i];
            // "**/" matches zero or more directory segments (including the empty
            // case, so "**/foo.json" matches "foo.json" at the root).
            if (c == '*' && i + 2 < glob.Length && glob[i + 1] == '*' && glob[i + 2] == '/')
            {
                sb.Append("(?:.*/)?");
                i += 2;
            }
            else if (c == '*' && i + 1 < glob.Length && glob[i + 1] == '*')
            {
                sb.Append(".*");
                i++;
            }
            else if (c == '*')
            {
                sb.Append("[^/]*");
            }
            else if (c == '?')
            {
                sb.Append("[^/]");
            }
            else if ("\\.+()[]{}^$|".IndexOf(c) >= 0)
            {
                sb.Append('\\').Append(c);
            }
            else
            {
                sb.Append(c);
            }
        }
        sb.Append('$');
        return new System.Text.RegularExpressions.Regex(sb.ToString(), System.Text.RegularExpressions.RegexOptions.Compiled);
    }
}
