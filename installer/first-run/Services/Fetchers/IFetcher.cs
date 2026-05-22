using System;
using System.Threading;
using System.Threading.Tasks;

namespace AIBox.FirstRun.Services.Fetchers;

/// <summary>Per-item progress event surfaced to the UI.</summary>
public sealed class FetchProgress
{
    public required string ItemId { get; init; }
    public required string DisplayName { get; init; }
    public long BytesDownloaded { get; init; }
    public long BytesTotal { get; init; }
    public string Phase { get; init; } = "downloading"; // downloading|extracting|verifying|done|retrying
    public string Detail { get; init; } = "";
}

/// <summary>Result of attempting to fetch one manifest item.</summary>
public sealed class FetchResult
{
    public required string ItemId { get; init; }
    public bool Success { get; init; }
    public string ErrorMessage { get; init; } = "";
    public Exception? Exception { get; init; }
    public bool IsRetryable { get; init; } = true;
    /// <summary>
    /// Item 28: Optional hint for per-source backoff override. Fetchers can set this
    /// to override the engine's default retry-delay for cases like HF's 429 rate-limits,
    /// which require much longer backoffs (30s/2m/5m vs. default 5s/15s/60s).
    /// </summary>
    public TimeSpan? RetryAfterHint { get; init; }
}

public interface IFetcher
{
    /// <summary>Which <see cref="ManifestItem.Source"/> values this fetcher handles.</summary>
    string Source { get; }

    /// <summary>Fetch one item end-to-end. Reports progress via <paramref name="progress"/>. Must be idempotent.</summary>
    Task<FetchResult> FetchAsync(
        ManifestItem item,
        InstallContext ctx,
        IProgress<FetchProgress> progress,
        CancellationToken ct);
}
