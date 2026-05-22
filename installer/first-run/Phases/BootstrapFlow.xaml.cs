using System;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using AIBox.FirstRun.Services;

namespace AIBox.FirstRun.Phases;

public partial class BootstrapFlow : UserControl, IPhase
{
    private readonly InstallContext _ctx;
    private readonly InstallState _state;
    private TaskCompletionSource<PhaseResult>? _tcs;
    private SubStep _current = SubStep.AdminPrompt;
    private CancellationToken _ct;

    private string? _adminUser;
    private string? _adminPass;

    private enum SubStep { AdminPrompt, GenerateEnv, ComposePull, SmokeTest, Finalize, Summary, Failed }

    public event EventHandler<string>? StatusChanged;

    public BootstrapFlow(InstallContext ctx, InstallState state)
    {
        InitializeComponent();
        _ctx = ctx;
        _state = state;
    }

    public Task<PhaseResult> RunAsync(CancellationToken ct)
    {
        _ct = ct;
        _tcs = new TaskCompletionSource<PhaseResult>();
        ShowAdminPrompt();
        return _tcs.Task;
    }

    private void ShowAdminPrompt()
    {
        StepHeader.Text = "Create your admin account";
        PrimaryButton.Content = "Continue";
        PrimaryButton.IsEnabled = true;
        SubStepHost.Content = new AdminPromptView();
        StatusChanged?.Invoke(this, "Set the admin credentials AIBox will use for the web login.");
    }

    private async void OnPrimaryClick(object sender, RoutedEventArgs e)
    {
        try
        {
            switch (_current)
            {
                case SubStep.AdminPrompt:
                    if (SubStepHost.Content is AdminPromptView view)
                    {
                        if (!view.TryGetInputs(out var user, out var pass, out var err))
                        {
                            MessageBox.Show(Window.GetWindow(this), err, "AIBox First Run",
                                MessageBoxButton.OK, MessageBoxImage.Warning);
                            return;
                        }
                        _adminUser = user;
                        _adminPass = pass;
                        _current = SubStep.GenerateEnv;
                        await RunSubStepsAsync().ConfigureAwait(true);
                    }
                    break;

                case SubStep.Summary:
                    _tcs!.TrySetResult(PhaseResult.Done());
                    break;

                case SubStep.Failed:
                    _tcs!.TrySetResult(PhaseResult.Failed(StepHeader.Text));
                    break;
            }
        }
        catch (Exception ex)
        {
            App.Logger.Error("Bootstrap step failed", ex);
            ShowError(ex);
        }
    }

    private async Task RunSubStepsAsync()
    {
        PrimaryButton.IsEnabled = false;
        var progressView = new StepProgressView();
        SubStepHost.Content = progressView;

        try
        {
            // 1) Generate secrets and write .env
            StepHeader.Text = "Writing secrets to stack/.env";
            progressView.AppendLine("Generating encryption key, session pepper, DNS admin password...");
            progressView.AppendLine($"Writing {_ctx.EnvFile}...");
            var envResult = EnvWriter.Write(_ctx.EnvFile, new EnvWriter.EnvInputs
            {
                AdminUsername = _adminUser!,
                AdminPassword = _adminPass!,
            }, App.Logger);
            EnvWriter.SaveDpapiAdminBlob(
                Path.Combine(_ctx.DataRoot, "admin-credentials.dpapi"),
                _adminPass!, App.Logger);
            if (envResult.AclLockedDown)
            {
                progressView.AppendLine("OK — .env written. ACLs locked to Administrators + SYSTEM.");
            }
            else
            {
                progressView.AppendLine("OK — .env written. WARNING: ACL lockdown failed (not elevated). Re-run as Administrator to secure the key.");
                App.Logger.Warn("ACL lockdown on .env was not applied — process is non-elevated. Banner shown to user.");
                AclWarningBanner.Visibility = Visibility.Visible;
            }
            _state.PhaseCStep = "env_written";
            StateStore.Save(_ctx.StateFile, _state);

            // 2) Wait for Docker, then docker compose pull
            StepHeader.Text = "Pulling container images";
            progressView.AppendLine("");
            progressView.AppendLine("Checking Docker daemon...");

            var docker = new DockerCli(_ctx.ComposeFile, App.Logger, line => progressView.AppendLine(line));
            if (!await docker.WaitForDaemonAsync(TimeSpan.FromSeconds(180), _ct).ConfigureAwait(true))
                throw new IOException("Docker Desktop is not running. Open Docker Desktop, wait for it to start, and re-launch First Run.");

            progressView.AppendLine("Docker is up. Running docker compose pull (~6 GB)...");
            var pull = await docker.ComposeAsync(new[] { "pull" }, _ct).ConfigureAwait(true);
            if (!pull.Ok)
                throw new IOException($"docker compose pull failed (exit {pull.ExitCode}). See log for details.");
            _state.PhaseCStep = "images_pulled";
            StateStore.Save(_ctx.StateFile, _state);

            // 3) Smoke test
            StepHeader.Text = "Verifying the stack starts";
            progressView.AppendLine("");
            progressView.AppendLine("Starting caddy + ai-control + llama for a smoke test...");
            var up = await docker.ComposeAsync(new[] { "up", "-d", "caddy", "ai-control", "llama" }, _ct).ConfigureAwait(true);
            if (!up.Ok)
                throw new IOException($"docker compose up -d failed (exit {up.ExitCode}). See log for details.");

            progressView.AppendLine("Polling http://localhost/ai/api/health for up to 90 seconds...");
            bool healthy = await WaitForHealthAsync("http://localhost/ai/api/health", TimeSpan.FromSeconds(90), _ct).ConfigureAwait(true);
            if (!healthy)
            {
                // Capture diagnostics before bringing it back down.
                progressView.AppendLine("Health endpoint did not respond. Capturing diagnostics...");
                await docker.ComposeAsync(new[] { "logs", "--tail=200" }, _ct).ConfigureAwait(true);
                await docker.ComposeAsync(new[] { "down" }, _ct).ConfigureAwait(true);
                throw new IOException(
                    "The stack started but did not become healthy in 90 seconds. " +
                    "Open the log folder for diagnostics, then re-run First Run when resolved.");
            }
            progressView.AppendLine("Health endpoint returned 200. Bringing the stack back to a stopped state...");
            await docker.ComposeAsync(new[] { "down" }, _ct).ConfigureAwait(true);
            _state.PhaseCStep = "smoke_passed";
            StateStore.Save(_ctx.StateFile, _state);

            // 4) Finalize: autostart task + shortcut rewrite
            StepHeader.Text = "Finalizing";
            progressView.AppendLine("");
            progressView.AppendLine("Registering AIBox-Puente-Startup scheduled task...");
            var autostart = new AutostartRegistrar(_ctx, App.Logger);
            var autostartOk = await autostart.RegisterAsync(_ct).ConfigureAwait(true);
            if (!autostartOk)
                progressView.AppendLine("Warning: install_autostart.ps1 reported a problem; continuing anyway.");

            progressView.AppendLine("Rewriting desktop shortcut to point at the Control Panel...");
            ShortcutWriter.DeleteShortcut(_ctx.DesktopShortcut);
            ShortcutWriter.WriteControlPanelShortcut(_ctx.ControlPanelShortcut, _ctx.InstallRoot, App.Logger);

            _state.PhaseCComplete = true;
            _state.PhaseCStep = "complete";
            _state.Touch();
            StateStore.Save(_ctx.StateFile, _state);

            // 5) Summary screen with admin password
            ShowSummary();
        }
        catch (OperationCanceledException)
        {
            _tcs!.TrySetResult(PhaseResult.Cancelled());
        }
        catch (Exception ex)
        {
            ShowError(ex);
        }
    }

    private void ShowSummary()
    {
        _current = SubStep.Summary;
        StepHeader.Text = "AIBox is installed and ready";
        PrimaryButton.Content = "Open Control Panel";
        PrimaryButton.IsEnabled = true;

        var view = new SummaryView();
        view.SetCredentials(_adminUser ?? "", _adminPass ?? "");
        SubStepHost.Content = view;
        StatusChanged?.Invoke(this, "Done. Open the Control Panel to start the stack.");
    }

    private void ShowError(Exception ex)
    {
        _current = SubStep.Failed;
        StepHeader.Text = "Setup did not complete";
        PrimaryButton.Content = "Close";
        PrimaryButton.IsEnabled = true;

        var tb = new TextBlock
        {
            Text = ex.Message + $"\n\nDetails: {App.LogDirectory}",
            TextWrapping = TextWrapping.Wrap,
            Foreground = (System.Windows.Media.Brush?)FindResource("ErrorBrush") ?? System.Windows.Media.Brushes.Red,
        };
        SubStepHost.Content = tb;
        StatusChanged?.Invoke(this, $"Failed: {ex.Message}");
    }

    private async Task<bool> WaitForHealthAsync(string url, TimeSpan timeout, CancellationToken ct)
    {
        using var http = new System.Net.Http.HttpClient { Timeout = TimeSpan.FromSeconds(5) };
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            ct.ThrowIfCancellationRequested();
            try
            {
                using var resp = await http.GetAsync(url, ct).ConfigureAwait(false);
                if ((int)resp.StatusCode == 200) return true;
            }
            catch { /* not up yet */ }
            await Task.Delay(TimeSpan.FromSeconds(3), ct).ConfigureAwait(false);
        }
        return false;
    }
}

internal sealed class StepProgressView : UserControl
{
    private readonly TextBox _log = new()
    {
        IsReadOnly = true,
        VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
        FontFamily = new System.Windows.Media.FontFamily("Cascadia Code, Consolas"),
        FontSize = 12,
        Background = System.Windows.Media.Brushes.Transparent,
        BorderThickness = new Thickness(0),
        TextWrapping = TextWrapping.NoWrap,
    };

    public StepProgressView()
    {
        Content = _log;
    }

    public void AppendLine(string line)
    {
        Dispatcher.Invoke(() =>
        {
            _log.AppendText(line + Environment.NewLine);
            _log.ScrollToEnd();
        });
    }
}
