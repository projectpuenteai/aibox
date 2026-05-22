using System;
using System.Collections.Generic;
using System.IO;
using System.Text.RegularExpressions;

namespace AIBox.FirstRun.Services;

/// <summary>
/// Thread-safe file-only logger with a redaction filter for secret values
/// the installer briefly handles (admin password, encryption key, DNS pwd,
/// HF tokens). One process owns one log file; rotation is best-effort and
/// happens once at startup.
/// </summary>
public sealed class FileLogger : IDisposable
{
    private readonly object _lock = new();
    private StreamWriter? _writer;
    private static readonly List<Regex> RedactPatterns = new()
    {
        new Regex(@"(?i)(password|pwd|pepper|encryption_key|master_key|token|secret)\s*=\s*\S+", RegexOptions.Compiled),
        new Regex(@"hf_[A-Za-z0-9]{30,}", RegexOptions.Compiled),
        new Regex(@"Bearer\s+[A-Za-z0-9._-]+", RegexOptions.Compiled),
    };

    public FileLogger(string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        _writer = new StreamWriter(new FileStream(
            path, FileMode.Append, FileAccess.Write, FileShare.Read)) { AutoFlush = true };
        WriteRaw("INFO", "Logger initialized: " + path);
    }

    public void Info(string msg) => WriteRaw("INFO", Redact(msg));
    public void Warn(string msg) => WriteRaw("WARN", Redact(msg));
    public void Error(string msg, Exception? ex = null)
    {
        var line = Redact(msg);
        if (ex != null) line += " :: " + Redact(ex.ToString());
        WriteRaw("ERROR", line);
    }
    public void Debug(string msg) => WriteRaw("DEBUG", Redact(msg));

    private void WriteRaw(string level, string msg)
    {
        lock (_lock)
        {
            if (_writer == null) return;
            _writer.WriteLine($"{DateTime.UtcNow:o} {level,-5} {msg}");
        }
    }

    private static string Redact(string msg)
    {
        foreach (var pattern in RedactPatterns)
            msg = pattern.Replace(msg, m =>
            {
                var idx = m.Value.IndexOf('=');
                return idx >= 0 ? m.Value.Substring(0, idx + 1) + "[redacted]" : "[redacted]";
            });
        return msg;
    }

    public void Dispose()
    {
        lock (_lock)
        {
            _writer?.Dispose();
            _writer = null;
        }
    }

    /// <summary>Delete all but the newest `keep` matching log files.</summary>
    public static void RotateOldLogs(string dir, string pattern, int keep)
    {
        try
        {
            var files = new DirectoryInfo(dir).GetFiles(pattern);
            Array.Sort(files, (a, b) => b.LastWriteTimeUtc.CompareTo(a.LastWriteTimeUtc));
            for (int i = keep; i < files.Length; i++)
            {
                try { files[i].Delete(); } catch { /* best-effort */ }
            }
        }
        catch
        {
            // best-effort
        }
    }
}
