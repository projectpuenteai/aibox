using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using AIBox.FirstRun.Services.Fetchers;

namespace AIBox.FirstRun.Services;

/// <summary>
/// Aggregate progress across all in-flight items.
/// </summary>
public sealed class AggregateProgress
{
    public long BytesDownloaded { get; init; }
    public long BytesTotal { get; init; }
    public int ItemsCompleted { get; init; }
    public int ItemsTotal { get; init; }
    public double BytesPerSecond { get; init; }
    public TimeSpan Eta { get; init; }
    public required Dictionary<string, FetchProgress> PerItem { get; init; }
}

/// <summary>
/// Schedules fetchers by source type. Honors per-source concurrency
/// limits (HF anonymous rate limits in particular) and per-item retries
/// with exponential backoff. Aggregates per-item progress into an
/// overall download view.
/// </summary>
public sealed class DownloadEngine
{
    private readonly Dictionary<string, IFetcher> _fetchers;
    private readonly FileLogger _log;
    private readonly InstallContext _ctx;
    private readonly InstallState _state;

    // Per-source concurrency caps. Tuned to upstream tolerance:
    //  - R2 is happy with parallel streams; cap at 4 to avoid swamping the user's pipe.
    //  - HF anonymous limit is harsh; serialize to 2 across the whole pool.
    //  - Kiwix mirrors dislike parallelism on the same file; one at a time.
    //  - Kolibri runs sequentially anyway (one docker exec at a time).
    private static readonly Dictionary<string, int> ConcurrencyCaps = new()
    {
        { "r2", 4 },
        { "huggingface", 2 },
        { "kiwix", 1 },
        { "kolibri_channel", 1 },
    };

    public DownloadEngine(IEnumerable<IFetcher> fetchers, InstallContext ctx, InstallState state, FileLogger log)
    {
        _fetchers = fetchers.ToDictionary(f => f.Source);
        _ctx = ctx;
        _state = state;
        _log = log;
    }

    public async Task RunAsync(
        Manifest manifest,
        IProgress<AggregateProgress> progress,
        CancellationToken ct)
    {
        var perItem = new ConcurrentDictionary<string, FetchProgress>();
        var itemsTotal = manifest.Items.Count;
        long totalSize = manifest.Items.Sum(EstimateSize);
        var sw = Stopwatch.StartNew();

        IProgress<FetchProgress> itemProgress = new Progress<FetchProgress>(p =>
        {
            perItem[p.ItemId] = p;
            var done = perItem.Values.Count(v => v.Phase == "done");
            long bytes = perItem.Values.Sum(v => v.BytesDownloaded);
            double speed = sw.Elapsed.TotalSeconds > 0 ? bytes / sw.Elapsed.TotalSeconds : 0;
            var remaining = Math.Max(0, totalSize - bytes);
            var eta = speed > 1024
                ? TimeSpan.FromSeconds(remaining / speed)
                : TimeSpan.Zero;
            progress.Report(new AggregateProgress
            {
                BytesDownloaded = bytes,
                BytesTotal = totalSize,
                ItemsCompleted = done,
                ItemsTotal = itemsTotal,
                BytesPerSecond = speed,
                Eta = eta,
                PerItem = new Dictionary<string, FetchProgress>(perItem),
            });
        });

        // Group by source so each source gets its own SemaphoreSlim. Within a
        // group, items are processed by a worker pool sized to the cap.
        var bySource = manifest.Items.GroupBy(i => i.Source).ToList();

        var allTasks = new List<Task>();
        foreach (var group in bySource)
        {
            var cap = ConcurrencyCaps.TryGetValue(group.Key, out var c) ? c : 1;
            allTasks.Add(RunSourceGroupAsync(group.Key, group.ToList(), cap, itemProgress, ct));
        }
        await Task.WhenAll(allTasks).ConfigureAwait(false);

        StateStore.Save(_ctx.StateFile, _state);
    }

    private async Task RunSourceGroupAsync(
        string source,
        List<ManifestItem> items,
        int cap,
        IProgress<FetchProgress> itemProgress,
        CancellationToken ct)
    {
        if (!_fetchers.TryGetValue(source, out var fetcher))
        {
            _log.Error($"No fetcher registered for source '{source}'; skipping {items.Count} item(s).");
            return;
        }

        using var sem = new SemaphoreSlim(cap);
        var work = items.Select(async item =>
        {
            await sem.WaitAsync(ct).ConfigureAwait(false);
            try
            {
                await RunOneWithRetryAsync(fetcher, item, itemProgress, ct).ConfigureAwait(false);
            }
            finally
            {
                sem.Release();
            }
        });
        await Task.WhenAll(work).ConfigureAwait(false);
    }

    private async Task RunOneWithRetryAsync(
        IFetcher fetcher,
        ManifestItem item,
        IProgress<FetchProgress> itemProgress,
        CancellationToken ct)
    {
        var itemState = _state.Items.GetValueOrDefault(item.Id) ?? new ItemState();
        _state.Items[item.Id] = itemState;

        if (itemState.Status == "done")
        {
            _log.Info($"Skipping already-completed item: {item.Id}");
            itemProgress.Report(new FetchProgress
            {
                ItemId = item.Id,
                DisplayName = item.Id,
                BytesDownloaded = itemState.BytesTotal,
                BytesTotal = itemState.BytesTotal,
                Phase = "done",
            });
            return;
        }

        // Item 28: Per-source backoff arrays. HF rate-limits are harsh; use longer backoffs
        // for 429 responses (per installerplan §6: 30s, 2m, 5m) than the default (5s, 15s, 60s).
        var defaultBackoffs = new[] { TimeSpan.FromSeconds(5), TimeSpan.FromSeconds(15), TimeSpan.FromSeconds(60) };
        var hfRateLimitBackoffs = new[] { TimeSpan.FromSeconds(30), TimeSpan.FromMinutes(2), TimeSpan.FromMinutes(5) };

        for (int attempt = 0; attempt <= defaultBackoffs.Length; attempt++)
        {
            itemState.Status = "downloading";
            itemState.RetryCount = attempt;
            _state.Touch();
            StateStore.Save(_ctx.StateFile, _state);

            var result = await fetcher.FetchAsync(item, _ctx, itemProgress, ct).ConfigureAwait(false);
            if (result.Success)
            {
                itemState.Status = "done";
                itemState.LastError = "";
                _state.Touch();
                StateStore.Save(_ctx.StateFile, _state);
                return;
            }

            itemState.LastError = result.ErrorMessage;
            itemState.LastErrorAtUtc = DateTime.UtcNow.ToString("o");
            _state.Touch();
            StateStore.Save(_ctx.StateFile, _state);

            if (!result.IsRetryable || attempt >= defaultBackoffs.Length)
            {
                itemState.Status = "failed";
                _log.Error($"Giving up on {item.Id} after {attempt + 1} attempt(s): {result.ErrorMessage}");
                StateStore.Save(_ctx.StateFile, _state);
                return;
            }

            // Use HF-specific backoff array when the HF fetcher signaled rate limiting.
            // The RetryAfterHint on a rate-limit result is just a flag — the actual
            // sleep duration comes from the HF backoff array, indexed by attempt.
            var isHfRateLimit = item.Source == "huggingface" && result.RetryAfterHint?.TotalSeconds > 0;
            var backoffs = isHfRateLimit ? hfRateLimitBackoffs : defaultBackoffs;

            // Honor an explicit Retry-After header (passed through as a non-trivial hint
            // by R2/Kiwix fetchers); otherwise step through the per-source backoff array.
            var sleepDuration = (!isHfRateLimit && result.RetryAfterHint is TimeSpan ra)
                ? ra
                : backoffs[attempt];
            _log.Warn($"Retry {attempt + 1}/{defaultBackoffs.Length} for {item.Id} after {sleepDuration.TotalSeconds}s: {result.ErrorMessage}");
            itemProgress.Report(new FetchProgress
            {
                ItemId = item.Id,
                DisplayName = item.Id,
                BytesDownloaded = itemState.BytesDownloaded,
                BytesTotal = itemState.BytesTotal,
                Phase = "retrying",
                Detail = result.ErrorMessage,
            });
            await Task.Delay(sleepDuration, ct).ConfigureAwait(false);
        }
    }

    private static long EstimateSize(ManifestItem item) => item switch
    {
        R2Item r2 => r2.SizeBytes,
        HuggingFaceItem hf when hf.IsMultiFile => hf.SizeBytesTotal,
        HuggingFaceItem hf => hf.SizeBytes,
        KiwixItem k => k.SizeBytes,
        KolibriChannelItem kc => kc.ApproxSizeBytes,
        _ => 0,
    };
}
