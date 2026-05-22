using System;
using System.Windows;

namespace AIBox.Uninstaller;

public partial class App : Application
{
    public static bool RanFromInno { get; private set; }

    protected override void OnStartup(StartupEventArgs e)
    {
        foreach (var arg in e.Args)
            if (string.Equals(arg, "--from-inno", StringComparison.OrdinalIgnoreCase))
                RanFromInno = true;
        base.OnStartup(e);
    }
}
