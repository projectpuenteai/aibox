using System;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;
using System.Windows.Threading;
using AIBox.AdminConsole.Services;

namespace AIBox.AdminConsole.ViewModels;

public enum StackState { Off, Starting, Ready, Stopping }

public sealed class MainViewModel : INotifyPropertyChanged
{
    private const int MaxConsoleLines = 500;

    private string _language = Translations.LangEs;
    private StackState _state = StackState.Off;
    private string? _ip;
    private string? _lastError;
    private CancellationTokenSource? _runCts;
    private readonly DispatcherTimer _statusTimer;

    public MainViewModel()
    {
        var (ssid, password) = EnvReader.ReadHotspotCredentials();
        Ssid = ssid;
        Password = password;
        Hostname = EnvReader.ReadHostname();

        ToggleCommand = new RelayCommand(ToggleAsync, () => ButtonEnabled);
        ClearConsoleCommand = new RelayCommand(() =>
        {
            Application.Current.Dispatcher.Invoke(() => ConsoleLines.Clear());
            return Task.CompletedTask;
        }, () => ConsoleLines.Count > 0);
        ConsoleLines.CollectionChanged += (_, _) => ClearConsoleCommand.RaiseCanExecuteChanged();

        _statusTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(3) };
        _statusTimer.Tick += (_, _) => OnStatusTick();

        // Probe once before timer starts so the UI opens in the right state
        // even when the scheduled-task autostart brought the stack up earlier.
        var probe = StackStatusProbe.Probe();
        if (probe.IsLive)
        {
            _ip = probe.HotspotIp;
            _state = StackState.Ready;
        }
    }

    public void StartStatusTimer() => _statusTimer.Start();
    public void StopStatusTimer() => _statusTimer.Stop();

    public bool IsTransitioning => _state == StackState.Starting || _state == StackState.Stopping;

    public string Ssid { get; }
    public string Password { get; }
    public string Hostname { get; }

    /// <summary>
    /// Live stream of stdout/stderr from the most recent up_stack / down_stack
    /// invocation. Bound to the console pane in the UI. Capped at 500 lines.
    /// </summary>
    public ObservableCollection<ConsoleLine> ConsoleLines { get; } = new();

    public string Language
    {
        get => _language;
        set
        {
            if (_language == value) return;
            _language = value;
            NotifyAllLocalized();
        }
    }

    public StackState State
    {
        get => _state;
        private set
        {
            if (_state == value) return;
            _state = value;
            NotifyAllStateful();
        }
    }

    public string IpDisplay => _ip ?? Translations.T(Language, "ipUnavailable");

    public string Title          => Translations.T(Language, "title");
    public string Subtitle       => Translations.T(Language, "subtitle");
    public string SsidLabel      => Translations.T(Language, "ssid");
    public string PasswordLabel  => Translations.T(Language, "password");
    public string IpLabel        => Translations.T(Language, "ipv4");
    public string ConsoleTitle   => Translations.T(Language, "consoleTitle");
    public string ClearBtnLabel  => Translations.T(Language, "clearBtn");

    public string ButtonLabel => State switch
    {
        StackState.Off      => Translations.T(Language, "start"),
        StackState.Starting => Translations.T(Language, "starting"),
        StackState.Ready    => Translations.T(Language, "stop"),
        StackState.Stopping => Translations.T(Language, "stopping"),
        _ => "",
    };

    public string PillText => State switch
    {
        StackState.Off      => Translations.T(Language, "pillOff"),
        StackState.Starting => Translations.T(Language, "pillStarting"),
        StackState.Ready    => Translations.T(Language, "pillReady"),
        StackState.Stopping => Translations.T(Language, "pillStopping"),
        _ => "",
    };

    public string Footer => _lastError is not null
        ? Translations.TF(Language, "footerError", _lastError)
        : State switch
        {
            StackState.Off      => Translations.T(Language, "footerOff"),
            StackState.Starting => Translations.T(Language, "footerStarting"),
            StackState.Ready    => Translations.TF(Language, "footerReady", Hostname),
            StackState.Stopping => Translations.T(Language, "footerStopping"),
            _ => "",
        };

    public Brush ButtonBrush => Resource(State switch
    {
        StackState.Off   => "HeroStartBrush",
        StackState.Ready => "HeroStopBrush",
        _                => "HeroBusyBrush",
    });

    public Brush PillBackground => Resource(State switch
    {
        StackState.Off   => "BrandIdleBg",
        StackState.Ready => "BrandSuccessBg",
        _                => "BrandWarnBg",
    });

    public Brush PillBorder => Resource(State switch
    {
        StackState.Off   => "BrandBorder",
        StackState.Ready => "BrandSuccessBd",
        _                => "BrandWarnBd",
    });

    public Brush PillForeground => Resource(State switch
    {
        StackState.Off   => "BrandTextSoft",
        StackState.Ready => "BrandSuccess",
        _                => "BrandWarn",
    });

    public Brush PillDot => Resource(State switch
    {
        StackState.Off   => "BrandIdleDot",
        StackState.Ready => "BrandSuccess",
        _                => "BrandWarn",
    });

    public bool ButtonEnabled => State == StackState.Off || State == StackState.Ready;

    public RelayCommand ToggleCommand { get; }
    public RelayCommand ClearConsoleCommand { get; }

    private static Brush Resource(string key) =>
        (Brush)Application.Current.FindResource(key);

    /// <summary>Marshals an incoming script line onto the UI thread.</summary>
    private void AppendConsoleLine(string stream, string text)
    {
        var line = new ConsoleLine(DateTime.Now, stream, text);
        var dispatcher = Application.Current?.Dispatcher;
        if (dispatcher == null || dispatcher.CheckAccess())
        {
            AddLine(line);
        }
        else
        {
            dispatcher.BeginInvoke(new Action(() => AddLine(line)));
        }
    }

    private void AddLine(ConsoleLine line)
    {
        ConsoleLines.Add(line);
        while (ConsoleLines.Count > MaxConsoleLines)
            ConsoleLines.RemoveAt(0);
    }

    private async Task ToggleAsync()
    {
        _runCts?.Dispose();
        _runCts = new CancellationTokenSource();

        switch (State)
        {
            case StackState.Off:
                _lastError = null;
                // Don't clear the console — the operator may want to scroll
                // back to a prior failure. They have a dedicated Clear button.
                State = StackState.Starting;
                Notify(nameof(Footer));

                var start = await StackController.StartStackAsync(AppendConsoleLine, _runCts.Token).ConfigureAwait(true);

                if (start.Ok)
                {
                    // Poll for the hotspot for up to 30 s. up_stack already does
                    // its own recovery when the ICS DNS toggle disrupts the
                    // adapter, but Mobile Hotspot can need a few seconds for
                    // WinRT to rebind 192.168.137.1 after that recovery
                    // finishes — we don't want to flash an error pill while
                    // it's still settling.
                    var probe = StackStatusProbe.Probe();
                    var deadline = DateTime.UtcNow.AddSeconds(30);
                    while (!probe.IsLive && DateTime.UtcNow < deadline)
                    {
                        try
                        {
                            await Task.Delay(TimeSpan.FromSeconds(2), _runCts.Token).ConfigureAwait(true);
                        }
                        catch (OperationCanceledException)
                        {
                            break;
                        }
                        probe = StackStatusProbe.Probe();
                    }

                    _ip = probe.HotspotIp;
                    State = probe.IsLive ? StackState.Ready : StackState.Off;
                    if (!probe.IsLive)
                        _lastError = Translations.T(Language, "hotspotNotDetected");
                }
                else
                {
                    _lastError = Translations.TF(Language, "scriptFailed", start.ErrorMessage ?? $"exit {start.ExitCode}");
                    State = StackState.Off;
                }
                Notify(nameof(IpDisplay));
                Notify(nameof(Footer));
                break;

            case StackState.Ready:
                _lastError = null;
                State = StackState.Stopping;
                Notify(nameof(Footer));

                var stop = await StackController.StopStackAsync(AppendConsoleLine, _runCts.Token).ConfigureAwait(true);
                _ip = null;
                if (!stop.Ok)
                    _lastError = Translations.TF(Language, "scriptFailed", stop.ErrorMessage ?? $"exit {stop.ExitCode}");
                State = StackState.Off;
                Notify(nameof(IpDisplay));
                Notify(nameof(Footer));
                break;
        }
    }

    private void OnStatusTick()
    {
        // While a script is in flight, ToggleAsync owns the state — don't second-guess it.
        if (IsTransitioning) return;

        var probe = StackStatusProbe.Probe();
        if (State == StackState.Off && probe.IsLive)
        {
            _ip = probe.HotspotIp;
            State = StackState.Ready;
            Notify(nameof(IpDisplay));
            Notify(nameof(Footer));
        }
        else if (State == StackState.Ready && !probe.IsLive)
        {
            _ip = null;
            State = StackState.Off;
            Notify(nameof(IpDisplay));
            Notify(nameof(Footer));
        }
        else if (State == StackState.Ready && probe.IsLive && probe.HotspotIp != _ip)
        {
            _ip = probe.HotspotIp;
            Notify(nameof(IpDisplay));
        }
    }

    private void NotifyAllLocalized()
    {
        Notify(nameof(Language));
        Notify(nameof(Title));
        Notify(nameof(Subtitle));
        Notify(nameof(SsidLabel));
        Notify(nameof(PasswordLabel));
        Notify(nameof(IpLabel));
        Notify(nameof(ConsoleTitle));
        Notify(nameof(ClearBtnLabel));
        Notify(nameof(ButtonLabel));
        Notify(nameof(PillText));
        Notify(nameof(Footer));
        Notify(nameof(IpDisplay));
    }

    private void NotifyAllStateful()
    {
        Notify(nameof(State));
        Notify(nameof(ButtonLabel));
        Notify(nameof(PillText));
        Notify(nameof(PillBackground));
        Notify(nameof(PillBorder));
        Notify(nameof(PillForeground));
        Notify(nameof(PillDot));
        Notify(nameof(ButtonBrush));
        Notify(nameof(ButtonEnabled));
        Notify(nameof(Footer));
        ToggleCommand.RaiseCanExecuteChanged();
    }

    public event PropertyChangedEventHandler? PropertyChanged;
    private void Notify([CallerMemberName] string? name = null) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}
