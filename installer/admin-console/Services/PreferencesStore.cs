using System;
using System.IO;
using System.Text.Json;

namespace AIBox.AdminConsole.Services;

/// <summary>
/// Round-trips backend-data/appdata/host-admin/ui-prefs.json. Shared with the
/// PowerShell control panel (aibox_control_ui.ps1) — same path, same JSON
/// shape — so a user who switches between the two keeps their language pick.
/// </summary>
public sealed class Preferences
{
    public string Language { get; set; } = Translations.LangEs;
    public double? WindowWidth { get; set; }
    public double? WindowHeight { get; set; }
}

public static class PreferencesStore
{
    private static readonly JsonSerializerOptions _options = new()
    {
        PropertyNameCaseInsensitive = true,
        WriteIndented = true,
    };

    public static Preferences Load()
    {
        try
        {
            var path = PathResolver.PrefsFile();
            if (!File.Exists(path)) return new Preferences();

            using var stream = File.OpenRead(path);
            var raw = JsonSerializer.Deserialize<RawPrefs>(stream, _options);
            if (raw is null) return new Preferences();

            var lang = string.IsNullOrWhiteSpace(raw.language) ? Translations.LangEs : raw.language!;
            if (lang != Translations.LangEs && lang != Translations.LangEn)
                lang = Translations.LangEs;

            return new Preferences
            {
                Language     = lang,
                WindowWidth  = raw.windowWidth  > 0 ? raw.windowWidth  : null,
                WindowHeight = raw.windowHeight > 0 ? raw.windowHeight : null,
            };
        }
        catch
        {
            return new Preferences();
        }
    }

    public static void Save(Preferences prefs)
    {
        try
        {
            var path = PathResolver.PrefsFile();
            var dir = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir))
                Directory.CreateDirectory(dir);

            var raw = new RawPrefs
            {
                language     = prefs.Language,
                windowWidth  = prefs.WindowWidth  ?? 0,
                windowHeight = prefs.WindowHeight ?? 0,
            };
            var json = JsonSerializer.Serialize(raw, _options);
            File.WriteAllText(path, json);
        }
        catch
        {
            // Persistence is best-effort.
        }
    }

    // Keys are lowercase to match the JSON written by the PS UI exactly.
    private sealed class RawPrefs
    {
        public string? language { get; set; }
        public double windowWidth { get; set; }
        public double windowHeight { get; set; }
    }
}
