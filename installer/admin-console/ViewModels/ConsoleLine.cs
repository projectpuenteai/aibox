using System;

namespace AIBox.AdminConsole.ViewModels;

/// <summary>
/// One line of captured script output for display in the live console pane.
/// Stream is "out" for stdout, "err" for stderr, "sys" for the .exe's own
/// run/exit markers.
/// </summary>
public sealed record ConsoleLine(DateTime Timestamp, string Stream, string Text)
{
    public string Display => $"{Timestamp:HH:mm:ss}  {Text}";
    public bool IsError => Stream == "err";
    public bool IsSystem => Stream == "sys";
}
