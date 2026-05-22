using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using AIBox.FirstRun.Services;
using AIBox.FirstRun.Services.Fetchers;

namespace AIBox.FirstRun.Phases;

public partial class DownloadPhase : UserControl, IPhase
{
    private readonly InstallContext _ctx;
    private readonly InstallState _state;
    private readonly ObservableCollection<ItemRow> _rows = new();
    private CancellationTokenSource? _cts;
    private TaskCompletionSource<PhaseResult>? _tcs;
    private Task? _engineTask;

    public event EventHandler<string>? StatusChanged;

    public DownloadPhase(InstallContext ctx, InstallState state)
    {
        InitializeComponent();
        _ctx = ctx;
        _state = state;
        ItemsList.ItemsSource = _rows;
    }

    public async Task<PhaseResult> RunAsync(CancellationToken ct)
    {
        _cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        _tcs = new TaskCompletionSource<PhaseResult>();

        _engineTask = Task.Run(async () =>
        {
            try
            {
                StatusChanged?.Invoke(this, "Fetching manifest...");
                using var http = BuildHttpClient();
                var mc = new ManifestClient(_ctx, http, App.Logger);
                var manifest = await mc.FetchAndVerifyAsync(_cts.Token).ConfigureAwait(false);

                var disk = GetFreeBytesForDataRoot();
                long needed = (long)(EstimateTotal(manifest) * 1.15);
                if (disk > 0 && disk < needed)
                    throw new IOException(
                        $"Not enough free space on the data drive. Need ~{needed / (1024L * 1024 * 1024)} GB, have {disk / (1024L * 1024 * 1024)} GB.");

                Dispatcher.Invoke(() => SeedRows(manifest));
                StatusChanged?.Invoke(this, "Downloading content...");

                var fetchers = new IFetcher[]
                {
                    new R2Fetcher(http, App.Logger),
                    new HuggingFaceFetcher(http, App.Logger),
                    new KiwixFetcher(http, App.Logger),
                    new KolibriFetcher(App.Logger, _ctx),
                };
                var engine = new DownloadEngine(fetchers, _ctx, _state, App.Logger);
                var progress = new Progress<AggregateProgress>(p => Dispatcher.Invoke(() => UpdateUi(p)));
                await engine.RunAsync(manifest, progress, _cts.Token).ConfigureAwait(false);

                var kolibri = fetchers.OfType<KolibriFetcher>().FirstOrDefault();
                if (kolibri != null)
                    await kolibri.StopKolibriAsync(_cts.Token).ConfigureAwait(false);

                var anyFailed = _state.Items.Values.Any(s => s.Status == "failed");
                if (anyFailed)
                {
                    _tcs.TrySetResult(PhaseResult.Failed(
                        "One or more downloads failed after retry. See log for details."));
                    return;
                }

                _state.PhaseBComplete = true;
                _state.Touch();
                StateStore.Save(_ctx.StateFile, _state);
                _tcs.TrySetResult(PhaseResult.Advance());
            }
            catch (OperationCanceledException)
            {
                _tcs?.TrySetResult(PhaseResult.Cancelled());
            }
            catch (Exception ex)
            {
                App.Logger.Error("DownloadPhase failed", ex);
                _tcs?.TrySetResult(PhaseResult.Failed(ex.Message, ex));
            }
        });

        return await _tcs.Task.ConfigureAwait(true);
    }

    private void SeedRows(Manifest manifest)
    {
        _rows.Clear();
        foreach (var item in manifest.Items)
            _rows.Add(new ItemRow { Id = item.Id, DisplayName = item.Id, BytesDownloaded = 0, BytesTotal = 1 });
    }

    private void UpdateUi(AggregateProgress agg)
    {
        foreach (var (id, fp) in agg.PerItem)
        {
            var row = FindRow(id);
            if (row == null) continue;
            row.DisplayName = fp.DisplayName;
            row.BytesDownloaded = fp.BytesDownloaded;
            row.BytesTotal = Math.Max(1, fp.BytesTotal);
            row.Notify();
        }

        var pct = agg.BytesTotal > 0
            ? Math.Min(100.0, (agg.BytesDownloaded * 100.0) / agg.BytesTotal)
            : 0;
        OverallProgress.Value = pct;
        OverallText.Text = $"{FormatGb(agg.BytesDownloaded)} / {FormatGb(agg.BytesTotal)}";
        var freeBytes = GetFreeBytesForDataRoot();
        StatsText.Text = $"Speed: {FormatRate(agg.BytesPerSecond)} · ETA: {FormatEta(agg.Eta)} · Disk free: {FormatGb(freeBytes)}";
    }

    private async void OnPauseClick(object sender, RoutedEventArgs e)
    {
        _cts?.Cancel();
        // Await the engine task with timeout so state.json is flushed before returning.
        if (_engineTask is not null)
        {
            try
            {
                await _engineTask.ConfigureAwait(true);
            }
            catch (OperationCanceledException)
            {
                // Expected on cancel.
            }
            catch (Exception ex)
            {
                // Any other engine failure during the cancel window — log and continue;
                // the user already clicked cancel, so we don't want to crash the UI.
                App.Logger?.Warn($"Engine task threw during cancel: {ex.GetType().Name}: {ex.Message}");
            }
        }
        StatusChanged?.Invoke(this, "Pausing... progress is saved. Close and re-open to resume.");
    }

    private async void OnCancelClick(object sender, RoutedEventArgs e)
    {
        var ans = MessageBox.Show(
            Window.GetWindow(this),
            "Cancel the download? Verified files will be kept; the rest is saved and resumes next time.",
            "AIBox First Run",
            MessageBoxButton.YesNo,
            MessageBoxImage.Question);
        if (ans == MessageBoxResult.Yes)
        {
            _cts?.Cancel();
            // Await the engine task with timeout so state.json is flushed before returning.
            if (_engineTask is not null)
            {
                try
                {
                    await _engineTask.ConfigureAwait(true);
                }
                catch (OperationCanceledException)
                {
                    // Expected on cancel.
                }
            }
        }
    }

    private ItemRow? FindRow(string id) => _rows.FirstOrDefault(r => r.Id == id);

    private long GetFreeBytesForDataRoot()
    {
        try
        {
            var di = new DriveInfo(Path.GetPathRoot(_ctx.DataRoot) ?? "C:\\");
            return di.AvailableFreeSpace;
        }
        catch { return 0; }
    }

    private static long EstimateTotal(Manifest m) => m.Items.Sum(i => i switch
    {
        R2Item r2 => r2.SizeBytes,
        HuggingFaceItem hf when hf.IsMultiFile => hf.SizeBytesTotal,
        HuggingFaceItem hf => hf.SizeBytes,
        KiwixItem k => k.SizeBytes,
        KolibriChannelItem kc => kc.ApproxSizeBytes,
        _ => 0L,
    });

    private static HttpClient BuildHttpClient()
    {
        var handler = new HttpClientHandler { AllowAutoRedirect = true, MaxAutomaticRedirections = 8 };
        var http = new HttpClient(handler) { Timeout = TimeSpan.FromMinutes(15) };
        http.DefaultRequestHeaders.UserAgent.ParseAdd("AIBoxFirstRun/0.1 (+https://github.com/ProjectPuente/aibox)");
        return http;
    }

    private static string FormatGb(long bytes)
    {
        const double gb = 1024.0 * 1024 * 1024;
        return bytes >= gb ? $"{bytes / gb:F1} GB" : $"{bytes / (1024.0 * 1024):F0} MB";
    }
    private static string FormatRate(double bps)
    {
        const double mb = 1024.0 * 1024;
        return bps >= mb ? $"{bps / mb:F1} MB/s" : $"{bps / 1024:F0} KB/s";
    }
    private static string FormatEta(TimeSpan eta)
    {
        if (eta == TimeSpan.Zero) return "--";
        if (eta.TotalHours >= 1) return $"{(int)eta.TotalHours}h {eta.Minutes}m";
        if (eta.TotalMinutes >= 1) return $"{(int)eta.TotalMinutes}m";
        return $"{(int)eta.TotalSeconds}s";
    }
}

public sealed class ItemRow : INotifyPropertyChanged
{
    public string Id { get; init; } = "";
    public string DisplayName { get; set; } = "";
    public long BytesDownloaded { get; set; }
    public long BytesTotal { get; set; } = 1;
    public string DisplayProgress => $"{Fmt(BytesDownloaded)} / {Fmt(BytesTotal)}";
    public event PropertyChangedEventHandler? PropertyChanged;
    public void Notify()
    {
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(DisplayName)));
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(BytesDownloaded)));
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(BytesTotal)));
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(DisplayProgress)));
    }
    private static string Fmt(long b)
    {
        const double mb = 1024.0 * 1024, gb = mb * 1024;
        if (b >= gb) return $"{b / gb:F1} GB";
        if (b >= mb) return $"{b / mb:F0} MB";
        return $"{b / 1024.0:F0} KB";
    }
}
