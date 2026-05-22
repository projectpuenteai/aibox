using System;
using System.IO;
using System.Threading;
using System.Windows;
using AIBox.AdminConsole.Services;

namespace AIBox.AdminConsole;

public partial class App : Application
{
    private static Mutex? _singleInstance;

    public static string LogDirectory { get; private set; } = "";
    public static string LogFile { get; private set; } = "";

    protected override void OnStartup(StartupEventArgs e)
    {
        bool createdNew;
        _singleInstance = new Mutex(true, @"Global\AIBoxAdminConsole.SingleInstance", out createdNew);
        if (!createdNew)
        {
            MessageBox.Show(
                "AIBox Admin Console is already running. Please use the open window.",
                "AIBox Admin Console",
                MessageBoxButton.OK,
                MessageBoxImage.Information);
            Shutdown(0);
            return;
        }

        try
        {
            var aiboxRoot = PathResolver.FindAiboxRoot();
            LogDirectory = Path.Combine(aiboxRoot, "backend-data", "appdata", "host-admin");
            Directory.CreateDirectory(LogDirectory);
            LogFile = Path.Combine(LogDirectory, "admin-console.log");
            FileLog.Append(LogFile, $"AIBox Admin Console starting (pid={Environment.ProcessId}, root={aiboxRoot}).");
        }
        catch (Exception ex)
        {
            MessageBox.Show(
                $"Could not locate the AIBox repository root.\n\n{ex.Message}\n\n" +
                "The .exe must live inside aibox/installer/admin-console/bin/... so it can find " +
                "the stack/docker-compose.yaml marker by walking up from its install directory.",
                "AIBox Admin Console",
                MessageBoxButton.OK,
                MessageBoxImage.Error);
            Shutdown(1);
            return;
        }

        DispatcherUnhandledException += (sender, args) =>
        {
            FileLog.Append(LogFile, $"Unhandled UI exception: {args.Exception}");
            MessageBox.Show(
                $"AIBox Admin Console hit an unexpected error:\n\n{args.Exception.Message}\n\n" +
                $"See log: {LogFile}",
                "AIBox Admin Console",
                MessageBoxButton.OK,
                MessageBoxImage.Error);
            args.Handled = true;
        };

        AppDomain.CurrentDomain.UnhandledException += (sender, args) =>
        {
            if (args.ExceptionObject is Exception ex)
                FileLog.Append(LogFile, $"Unhandled domain exception: {ex}");
        };

        base.OnStartup(e);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        if (!string.IsNullOrEmpty(LogFile))
            FileLog.Append(LogFile, $"AIBox Admin Console exiting (code={e.ApplicationExitCode}).");
        _singleInstance?.ReleaseMutex();
        _singleInstance?.Dispose();
        base.OnExit(e);
    }
}
