using System;
using System.IO;
using System.Reflection;

namespace AIBox.FirstRun.Services;

/// <summary>
/// Resolves the install root, data root, and other paths the rest of the
/// First Run app needs. Source of truth is the Inno-written
/// %ProgramData%\AIBox\install-state.json when present; otherwise we
/// fall back to defaults so a developer can run the app from a checkout.
/// </summary>
public sealed class InstallContext
{
    public required string InstallRoot { get; init; }
    public required string DataRoot { get; init; }
    public required string StateFile { get; init; }
    public required string EnvFile { get; init; }
    public required string ComposeFile { get; init; }
    public required string LogDir { get; init; }
    public required string TempDir { get; init; }
    public required Uri ManifestUrl { get; init; }
    public required Uri ManifestSigUrl { get; init; }
    public required Uri ManifestFallbackUrl { get; init; }

    /// <summary>Path to the manifest bundled alongside the installed app tree.
    /// When present, ManifestClient prefers this over the URL fetch (offline-first).</summary>
    public required string BundledManifestPath { get; init; }
    public required string BundledManifestSigPath { get; init; }

    public string DesktopShortcut => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
        "AIBox First Run.lnk");

    public string ControlPanelShortcut => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
        "AIBox Control Panel.lnk");

    public static InstallContext Discover()
    {
        var dataRoot = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            "AIBox");
        Directory.CreateDirectory(dataRoot);

        string installRoot;
        string stateFile = Path.Combine(dataRoot, "install-state.json");

        if (File.Exists(stateFile))
        {
            try
            {
                var hint = StateStore.Load(stateFile);
                installRoot = string.IsNullOrEmpty(hint.InstallRoot)
                    ? DefaultInstallRoot()
                    : hint.InstallRoot;
            }
            catch
            {
                installRoot = DefaultInstallRoot();
            }
        }
        else
        {
            installRoot = DefaultInstallRoot();
        }

        var envFile = Path.Combine(installRoot, "aibox", "stack", ".env");
        var composeFile = Path.Combine(installRoot, "aibox", "stack", "docker-compose.yaml");
        var logDir = Path.Combine(dataRoot, "logs");
        var tempDir = Path.Combine(dataRoot, "tmp");
        Directory.CreateDirectory(logDir);
        Directory.CreateDirectory(tempDir);

        var manifestVersion = ReadEmbeddedManifestVersion();
        var manifestBase = ReadEmbeddedManifestBase();
        var manifestUrl = new Uri($"{manifestBase}/manifest-{manifestVersion}.json");
        var manifestSigUrl = new Uri($"{manifestBase}/manifest-{manifestVersion}.json.sig");
        var manifestFallback = new Uri(
            $"https://github.com/ProjectPuente/aibox/releases/download/v{manifestVersion}/manifest-{manifestVersion}.json");
        var bundledManifestPath    = Path.Combine(installRoot, "manifest", $"manifest-{manifestVersion}.json");
        var bundledManifestSigPath = bundledManifestPath + ".sig";

        return new InstallContext
        {
            InstallRoot = installRoot,
            DataRoot = dataRoot,
            StateFile = stateFile,
            EnvFile = envFile,
            ComposeFile = composeFile,
            LogDir = logDir,
            TempDir = tempDir,
            ManifestUrl = manifestUrl,
            ManifestSigUrl = manifestSigUrl,
            ManifestFallbackUrl = manifestFallback,
            BundledManifestPath = bundledManifestPath,
            BundledManifestSigPath = bundledManifestSigPath,
        };
    }

    private static string DefaultInstallRoot()
    {
        // ProgramFiles\AIBox on a real install; when running from a checkout, walk
        // up from the executable until we find the aibox/ subdir.
        var exeDir = Path.GetDirectoryName(Assembly.GetEntryAssembly()!.Location) ?? "";
        var probe = new DirectoryInfo(exeDir);
        while (probe != null)
        {
            if (Directory.Exists(Path.Combine(probe.FullName, "aibox", "stack")))
                return probe.FullName;
            probe = probe.Parent;
        }

        var pf = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
        return Path.Combine(pf, "AIBox");
    }

    private static string ReadEmbeddedManifestVersion()
    {
        // CI replaces this constant at build time. Default to the sample
        // manifest version for dev builds.
        return BuildConstants.ManifestVersion;
    }

    private static string ReadEmbeddedManifestBase()
    {
        return BuildConstants.ManifestBaseUrl;
    }
}
