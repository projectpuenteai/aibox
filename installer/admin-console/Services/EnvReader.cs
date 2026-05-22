using System;
using System.Collections.Generic;
using System.IO;

namespace AIBox.AdminConsole.Services;

/// <summary>
/// Reads simple KEY=VALUE pairs from aibox/stack/.env. Mirrors the
/// Read-EnvValue helper in aibox_control_ui.ps1: process-environment override
/// wins, otherwise the value from .env, otherwise the supplied default. Strips
/// matched surrounding double-quotes or single-quotes.
/// </summary>
public static class EnvReader
{
    public static string Read(string key, string defaultValue = "")
    {
        var fromEnv = Environment.GetEnvironmentVariable(key);
        if (!string.IsNullOrWhiteSpace(fromEnv)) return fromEnv;

        string envPath;
        try { envPath = PathResolver.StackEnvFile(); }
        catch { return defaultValue; }

        if (!File.Exists(envPath)) return defaultValue;

        try
        {
            foreach (var raw in File.ReadAllLines(envPath))
            {
                var line = raw.TrimStart();
                if (line.Length == 0 || line.StartsWith('#')) continue;

                var eq = line.IndexOf('=');
                if (eq <= 0) continue;

                var k = line.Substring(0, eq).Trim();
                if (!string.Equals(k, key, StringComparison.Ordinal)) continue;

                var v = line.Substring(eq + 1).Trim();
                if (v.Length >= 2)
                {
                    if ((v[0] == '"' && v[^1] == '"') ||
                        (v[0] == '\'' && v[^1] == '\''))
                    {
                        v = v.Substring(1, v.Length - 2);
                    }
                }
                return string.IsNullOrWhiteSpace(v) ? defaultValue : v;
            }
        }
        catch
        {
            // Best-effort; fall back to default.
        }

        return defaultValue;
    }

    public static (string Ssid, string Password) ReadHotspotCredentials()
    {
        var ssid = Read("HOTSPOT_SSID", "AIBox-Puente");
        var key  = Read("HOTSPOT_KEY",  "puente1234");
        return (ssid, key);
    }

    public static string ReadHostname() => Read("OFFLINE_HOSTNAME", "puente.link");
}
