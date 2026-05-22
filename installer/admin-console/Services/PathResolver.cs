using System;
using System.IO;

namespace AIBox.AdminConsole.Services;

/// <summary>
/// Locates the aibox/ repository root by walking up from the running .exe
/// directory looking for the stack/docker-compose.yaml marker.
/// </summary>
public static class PathResolver
{
    private static string? _cached;

    public static string FindAiboxRoot()
    {
        if (_cached is not null) return _cached;

        var probes = new[]
        {
            AppContext.BaseDirectory,
            Directory.GetCurrentDirectory(),
        };

        foreach (var start in probes)
        {
            var dir = new DirectoryInfo(start);
            while (dir is not null)
            {
                var marker = Path.Combine(dir.FullName, "stack", "docker-compose.yaml");
                if (File.Exists(marker))
                {
                    _cached = dir.FullName;
                    return _cached;
                }
                dir = dir.Parent;
            }
        }

        throw new DirectoryNotFoundException(
            $"Could not find the aibox repository root from {AppContext.BaseDirectory}. " +
            "Looked for 'stack/docker-compose.yaml' walking upward from the .exe location and the current directory.");
    }

    public static string UpScript() =>
        Path.Combine(FindAiboxRoot(), "tools", "llama-runtime", "scripts", "up_stack.ps1");

    public static string DownScript() =>
        Path.Combine(FindAiboxRoot(), "tools", "llama-runtime", "scripts", "down_stack.ps1");

    public static string StackEnvFile() =>
        Path.Combine(FindAiboxRoot(), "stack", ".env");

    public static string PrefsFile() =>
        Path.Combine(FindAiboxRoot(), "backend-data", "appdata", "host-admin", "ui-prefs.json");
}
