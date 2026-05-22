using System;
using System.IO;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using AIBox.FirstRun.Phases;
using AIBox.FirstRun.Services;

namespace AIBox.FirstRun;

public partial class MainWindow : Window
{
    private readonly CancellationTokenSource _shutdownCts = new();
    private InstallContext? _context;
    private Task? _stateMachineTask;

    public MainWindow()
    {
        InitializeComponent();
        VersionText.Text = $"v{Assembly.GetExecutingAssembly().GetName().Version?.ToString(3) ?? "?"}";
        Loaded += OnLoadedAsync;
        Closing += OnClosing;
    }

    private async void OnLoadedAsync(object sender, RoutedEventArgs e)
    {
        try
        {
            _context = InstallContext.Discover();
            App.Logger.Info($"Install root: {_context.InstallRoot}");
            App.Logger.Info($"Data root:    {_context.DataRoot}");
            App.Logger.Info($"State file:   {_context.StateFile}");

            _stateMachineTask = RunStateMachineAsync(_shutdownCts.Token);
            await _stateMachineTask.ConfigureAwait(true);
        }
        catch (OperationCanceledException)
        {
            App.Logger.Info("First Run cancelled by user.");
        }
        catch (Exception ex)
        {
            App.Logger.Error("Fatal error during First Run.", ex);
            ShowError("AIBox First Run encountered an unrecoverable error.", ex);
        }
    }

    private async void OnClosing(object? sender, System.ComponentModel.CancelEventArgs e)
    {
        _shutdownCts.Cancel();
        // Wait briefly for the state machine task to flush state.json before closing.
        if (_stateMachineTask is not null)
        {
            try
            {
                await Task.WhenAny(_stateMachineTask, Task.Delay(5000)).ConfigureAwait(true);
            }
            catch { /* Ignore cancellation/timeout; close anyway. */ }
        }
    }

    private async Task RunStateMachineAsync(CancellationToken ct)
    {
        if (_context is null) throw new InvalidOperationException("Context not initialized.");
        var loadResult = StateStore.LoadWithStatus(_context.StateFile);
        var state = loadResult.State;

        if (loadResult.Status == StateStore.LoadStatus.Corrupt)
        {
            ShowFatal(
                "Install state was unreadable",
                "The install-state.json file was corrupted and could not be parsed. " +
                $"A backup was saved at {loadResult.CorruptBackupPath}. " +
                "Re-run AIBox-Setup-<version>.exe to recreate the file, then launch First Run again.");
            return;
        }

        if (state.PhaseCComplete)
        {
            HeaderText.Text = "AIBox is already installed";
            SubheaderText.Text = "Opening the Control Panel...";
            PhaseHost.Content = new DonePhase(_context);
            return;
        }

        if (!state.PhaseAComplete)
        {
            // Differentiate "no state file" (Inno never ran) from "state file
            // exists but Phase A is somehow false" (likely tampering or
            // partial write that wasn't caught by JsonException above).
            var detail = loadResult.Status == StateStore.LoadStatus.Missing
                ? "Phase A (file extraction by AIBox-Setup.exe) has not completed. " +
                  "Run AIBox-Setup-<version>.exe before launching First Run."
                : "The install-state.json file is present but reports Phase A is " +
                  "incomplete. Re-run AIBox-Setup-<version>.exe to repair it.";
            ShowFatal("Setup is incomplete", detail);
            return;
        }

        IPhase phase;
        if (!state.PhaseBComplete)
        {
            HeaderText.Text = "Step 1 of 2 — Downloading required content";
            SubheaderText.Text = "AIBox needs ~100 GB of models, Wikipedia, and the search index.";
            phase = new DownloadPhase(_context, state);
        }
        else
        {
            HeaderText.Text = "Step 2 of 2 — Final setup";
            SubheaderText.Text = "Create your admin account and verify the stack starts.";
            phase = new BootstrapFlow(_context, state);
        }

        SetPhaseControl(phase);
        SetStatus("Working...");

        phase.StatusChanged += (s, msg) => Dispatcher.Invoke(() => SetStatus(msg));
        var result = await phase.RunAsync(ct).ConfigureAwait(true);

        if (result.Kind == PhaseResultKind.AdvanceToNextPhase)
        {
            // re-enter the state machine; persisted state has advanced
            await RunStateMachineAsync(ct).ConfigureAwait(true);
            return;
        }

        if (result.Kind == PhaseResultKind.Done)
        {
            HeaderText.Text = "AIBox is ready";
            SubheaderText.Text = "Open the Control Panel to start the stack.";
            PhaseHost.Content = new DonePhase(_context);
            return;
        }

        if (result.Kind == PhaseResultKind.Cancelled)
        {
            SetStatus("Cancelled. Re-open AIBox First Run when ready — progress is saved.");
            return;
        }

        if (result.Kind == PhaseResultKind.Failed)
        {
            ShowError($"This step did not complete: {result.Message}", result.Exception);
            return;
        }
    }

    private void SetPhaseControl(IPhase phase)
    {
        if (phase is UserControl uc)
            PhaseHost.Content = uc;
        else
            PhaseHost.Content = new TextBlock
            {
                Text = phase.GetType().Name + " (no view)",
                Foreground = (System.Windows.Media.Brush?)FindResource("MutedBrush") ?? System.Windows.Media.Brushes.Gray,
            };
    }

    private void SetStatus(string msg)
    {
        StatusText.Text = msg;
    }

    private void ShowError(string heading, Exception? ex)
    {
        var detail = ex is null ? "" : $"\n\n{ex.GetType().Name}: {ex.Message}";
        MessageBox.Show(
            this,
            heading + detail + $"\n\nLog folder: {App.LogDirectory}",
            "AIBox First Run",
            MessageBoxButton.OK,
            MessageBoxImage.Error);
    }

    private void ShowFatal(string title, string message)
    {
        HeaderText.Text = title;
        SubheaderText.Text = message;
        PhaseHost.Content = null;
    }
}
