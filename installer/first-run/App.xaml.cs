using System;
using System.IO;
using System.Threading;
using System.Windows;
using AIBox.FirstRun.Services;

namespace AIBox.FirstRun;

public partial class App : Application
{
    private static Mutex? _singleInstance;
    public static string LogDirectory { get; private set; } = "";
    public static FileLogger Logger { get; private set; } = null!;

    protected override void OnStartup(StartupEventArgs e)
    {
        bool createdNew;
        _singleInstance = new Mutex(true, @"Global\AIBoxFirstRun.SingleInstance", out createdNew);
        if (!createdNew)
        {
            MessageBox.Show(
                "AIBox First Run is already running. Please close the existing window first.",
                "AIBox First Run",
                MessageBoxButton.OK,
                MessageBoxImage.Information);
            Shutdown(0);
            return;
        }

        LogDirectory = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            "AIBox",
            "logs");
        Directory.CreateDirectory(LogDirectory);

        Logger = new FileLogger(Path.Combine(
            LogDirectory,
            $"first-run-{DateTime.Now:yyyyMMdd-HHmmss}.log"));
        Logger.Info($"AIBox First Run starting (pid={Environment.ProcessId}).");

        FileLogger.RotateOldLogs(LogDirectory, "first-run-*.log", keep: 10);

        DispatcherUnhandledException += (sender, args) =>
        {
            Logger.Error("Unhandled UI exception", args.Exception);
            MessageBox.Show(
                $"AIBox First Run hit an unexpected error:\n\n{args.Exception.Message}\n\n" +
                $"Details have been written to:\n{LogDirectory}",
                "AIBox First Run",
                MessageBoxButton.OK,
                MessageBoxImage.Error);
            args.Handled = true;
        };

        AppDomain.CurrentDomain.UnhandledException += (sender, args) =>
        {
            if (args.ExceptionObject is Exception ex)
                Logger.Error("Unhandled domain exception", ex);
        };

        base.OnStartup(e);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        Logger?.Info($"AIBox First Run exiting (code={e.ApplicationExitCode}).");
        Logger?.Dispose();
        _singleInstance?.ReleaseMutex();
        _singleInstance?.Dispose();
        base.OnExit(e);
    }
}
