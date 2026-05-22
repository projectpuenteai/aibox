using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using System.Xml.Linq;

namespace AIBox.FirstRun.Services.Fetchers;

/// <summary>
/// Fetches Wikipedia ZIM files from Kiwix mirrors. Resolution order:
///   1. The manifest's fallback_url (download.kiwix.org direct).
///   2. Directory-listing scrape on download.kiwix.org/zim/wikipedia/ (primary fallback).
///      This approach is more reliable than the OPDS catalog, which has historically
///      returned zero results for the name filters we use.
///   3. OPDS catalog at opds.library.kiwix.org (low-priority probe only).
///      Even when the catalog returns entries, they often point at .zim.meta4 metalink
///      files rather than direct URLs. Metalink resolution adds complexity and the
///      directory-listing scrape is sufficient since Kiwix exposes canonical filenames
///      at download.kiwix.org/zim/wikipedia/ for all current dumps.
/// The .sha256 sidecar (Kiwix-served) is the authoritative checksum —
/// the manifest does NOT inline ZIM hashes so a Kiwix-side rebuild
/// doesn't force a manifest reissue.
/// </summary>
public sealed class KiwixFetcher : IFetcher
{
    private readonly HttpClient _http;
    private readonly FileLogger _log;
    private const string CatalogBase = "https://opds.library.kiwix.org";
    private const string DirectoryBase = "https://download.kiwix.org/zim/wikipedia/";

    public string Source => "kiwix";

    public KiwixFetcher(HttpClient http, FileLogger log)
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
        if (item is not KiwixItem k)
            return new FetchResult { ItemId = item.Id, Success = false, IsRetryable = false,
                ErrorMessage = $"KiwixFetcher received non-Kiwix item (source='{item.Source}')." };

        try
        {
            var resolved = await ResolveDownloadUrlAsync(k, ct).ConfigureAwait(false);
            _log.Info($"Kiwix resolved {k.Id} -> {resolved.DownloadUrl} (sha256 sidecar: {resolved.Sha256Url ?? "<none>"})");

            string? expectedSha = null;
            if (!string.IsNullOrEmpty(resolved.Sha256Url))
            {
                expectedSha = await FetchSha256SidecarAsync(new Uri(resolved.Sha256Url), ct).ConfigureAwait(false);
                _log.Info($"Kiwix sidecar SHA256 for {k.Id}: {expectedSha}");
            }

            var target = Path.Combine(ctx.DataRoot, k.Target.Replace('/', Path.DirectorySeparatorChar));

            var bytesP = new Progress<long>(b => progress.Report(new FetchProgress
            {
                ItemId = item.Id,
                DisplayName = Path.GetFileName(k.Target),
                BytesDownloaded = b,
                BytesTotal = k.SizeBytes,
                Phase = "downloading",
            }));

            await HttpRangeDownloader.DownloadAsync(
                _http, new Uri(resolved.DownloadUrl), target,
                expectedSha, k.SizeBytes, bytesP, _log, ct).ConfigureAwait(false);

            progress.Report(new FetchProgress
            {
                ItemId = item.Id,
                DisplayName = Path.GetFileName(k.Target),
                BytesDownloaded = k.SizeBytes,
                BytesTotal = k.SizeBytes,
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
            _log.Error($"Kiwix fetch failed for {item.Id}", ex);
            return new FetchResult
            {
                ItemId = item.Id, Success = false, IsRetryable = true,
                ErrorMessage = ex.Message, Exception = ex,
            };
        }
    }

    private sealed record Resolved(string DownloadUrl, string? Sha256Url);

    private async Task<Resolved> ResolveDownloadUrlAsync(KiwixItem k, CancellationToken ct)
    {
        // Item 19 + Item 25: Reordered resolution path.
        // PRIMARY: manifest fallback_url (direct download.kiwix.org URL).
        // SECONDARY: directory-listing scrape on download.kiwix.org/zim/wikipedia/
        //           (stable, no API filtering issues, handles all current Kiwix dumps).
        // TERTIARY: OPDS catalog (low-priority probe only).
        //          The catalog's name= filter returns totalResults=0 for queries we use
        //          (confirmed live). Even when it returns entries, they often point at
        //          .zim.meta4 metalink files, adding complexity. We skip it unless
        //          it returns non-zero results AND a reachable mirror, to avoid dead-weight calls.

        // 1) Try the manifest fallback URL.
        if (!string.IsNullOrEmpty(k.FallbackUrl))
        {
            if (await IsReachableAsync(k.FallbackUrl, ct).ConfigureAwait(false))
                return new Resolved(k.FallbackUrl, k.Sha256Url);
            _log.Warn($"Fallback URL {k.FallbackUrl} returned non-2xx on HEAD; trying directory scrape.");
        }

        // 2) Directory listing on download.kiwix.org/zim/wikipedia/ (primary fallback).
        if (k.CatalogQuery != null && !string.IsNullOrEmpty(k.CatalogQuery.Name))
        {
            var fromDir = await FindInDirectoryListingAsync(k.CatalogQuery.Name, ct).ConfigureAwait(false);
            if (fromDir != null)
                return fromDir;
        }

        // 3) Try the OPDS catalog as a low-priority probe (skip without warning if empty).
        if (k.CatalogQuery != null && !string.IsNullOrEmpty(k.CatalogQuery.Name))
        {
            try
            {
                var mirrors = await QueryCatalogAsync(k.CatalogQuery.Name, ct).ConfigureAwait(false);
                if (mirrors.Count > 0) // Only proceed if OPDS returns entries (live: usually 0).
                {
                    foreach (var (mirror, shaUrl) in mirrors)
                    {
                        if (await IsReachableAsync(mirror, ct).ConfigureAwait(false))
                            return new Resolved(mirror, shaUrl);
                    }
                    _log.Warn($"Catalog returned {mirrors.Count} mirror(s) for '{k.CatalogQuery.Name}' but none answered HEAD.");
                }
                // If OPDS returns 0 entries, skip without warning — this is expected behavior.
            }
            catch (Exception ex)
            {
                _log.Warn($"Catalog lookup failed ({ex.GetType().Name}): {ex.Message}. Skipping OPDS probe.");
            }
        }

        throw new IOException($"Could not resolve a download URL for Kiwix item {k.Id} (all sources failed).");
    }

    private async Task<List<(string MirrorUrl, string? Sha256Url)>> QueryCatalogAsync(string name, CancellationToken ct)
    {
        var url = new Uri($"{CatalogBase}/catalog/v2/entries?count=-1&name={Uri.EscapeDataString(name)}");
        using var resp = await _http.GetAsync(url, ct).ConfigureAwait(false);
        resp.EnsureSuccessStatusCode();
        var xml = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
        var doc = XDocument.Parse(xml);

        XNamespace atom = "http://www.w3.org/2005/Atom";
        var mirrors = new List<(string, string?)>();
        foreach (var entry in doc.Descendants(atom + "entry"))
        {
            string? primary = null;
            string? shaUrl = null;
            foreach (var link in entry.Elements(atom + "link"))
            {
                var rel = (string?)link.Attribute("rel");
                var href = (string?)link.Attribute("href");
                var type = (string?)link.Attribute("type");
                if (string.IsNullOrEmpty(href)) continue;
                if (rel == "http://opds-spec.org/acquisition/open-access" ||
                    rel == "alternate" ||
                    (rel == null && type?.Contains("zim", StringComparison.OrdinalIgnoreCase) == true))
                {
                    if (primary == null) primary = href;
                    else mirrors.Add((href, null));
                }
                if (rel == "describedby" && href!.EndsWith(".sha256", StringComparison.OrdinalIgnoreCase))
                    shaUrl = href;
            }
            if (primary != null) mirrors.Insert(0, (primary, shaUrl));
        }
        // Dedupe + carry sidecar forward if only listed once.
        var unique = new List<(string, string?)>();
        var seen = new HashSet<string>();
        string? carriedSha = mirrors.FirstOrDefault(m => m.Item2 != null).Item2;
        foreach (var m in mirrors)
        {
            if (seen.Add(m.Item1))
                unique.Add((m.Item1, m.Item2 ?? carriedSha));
        }
        return unique;
    }

    private async Task<bool> IsReachableAsync(string url, CancellationToken ct)
    {
        try
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(TimeSpan.FromSeconds(5));
            using var req = new HttpRequestMessage(HttpMethod.Head, url);
            using var resp = await _http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, cts.Token).ConfigureAwait(false);
            return (int)resp.StatusCode is >= 200 and < 400;
        }
        catch
        {
            return false;
        }
    }

    private async Task<Resolved?> FindInDirectoryListingAsync(string prefix, CancellationToken ct)
    {
        try
        {
            var html = await _http.GetStringAsync(DirectoryBase, ct).ConfigureAwait(false);
            // Crude but stable scrape: any <a href="...{prefix}_YYYY-MM.zim"> link.
            var pattern = new System.Text.RegularExpressions.Regex(
                $@"href=""(?<name>{System.Text.RegularExpressions.Regex.Escape(prefix)}[_-][0-9]{{4}}-[0-9]{{2}}\.zim)""",
                System.Text.RegularExpressions.RegexOptions.IgnoreCase);
            var matches = pattern.Matches(html)
                .Select(m => m.Groups["name"].Value)
                .Distinct()
                .OrderByDescending(name => name) // latest dump date sorts last lexically, which descending puts first
                .ToList();
            if (matches.Count == 0)
            {
                _log.Warn($"Directory listing on {DirectoryBase} had no entries matching '{prefix}'.");
                return null;
            }
            var pick = matches[0];
            return new Resolved($"{DirectoryBase}{pick}", $"{DirectoryBase}{pick}.sha256");
        }
        catch (Exception ex)
        {
            _log.Warn($"Directory scrape on {DirectoryBase} failed: {ex.Message}");
            return null;
        }
    }

    private async Task<string?> FetchSha256SidecarAsync(Uri url, CancellationToken ct)
    {
        var text = await _http.GetStringAsync(url, ct).ConfigureAwait(false);
        // Sidecars look like "<hex>  <filename>\n"; take just the hex.
        var space = text.IndexOfAny(new[] { ' ', '\t', '\n', '\r' });
        var hex = (space > 0 ? text[..space] : text).Trim().ToLowerInvariant();
        return hex.Length == 64 ? hex : null;
    }
}
