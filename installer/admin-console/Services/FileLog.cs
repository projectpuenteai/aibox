using System;
using System.IO;
using System.Text;

namespace AIBox.AdminConsole.Services;

/// <summary>
/// Append-only log writer. Thread-safe via a static lock; line rate is low
/// enough (a few per second during script runs) that lock contention is
/// negligible.
/// </summary>
public static class FileLog
{
    private static readonly object _gate = new();

    public static void Append(string path, string line)
    {
        try
        {
            lock (_gate)
            {
                var dir = Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir))
                    Directory.CreateDirectory(dir);

                var stamp = DateTimeOffset.Now.ToString("yyyy-MM-ddTHH:mm:sszzz");
                File.AppendAllText(path, $"{stamp} {line}{Environment.NewLine}", Encoding.UTF8);

                // Soft cap at ~5 MB: if the file is bigger, truncate to the last 4 MB.
                var fi = new FileInfo(path);
                if (fi.Exists && fi.Length > 5L * 1024 * 1024)
                {
                    var all = File.ReadAllBytes(path);
                    var keep = new byte[4L * 1024 * 1024];
                    Buffer.BlockCopy(all, all.Length - keep.Length, keep, 0, keep.Length);
                    File.WriteAllBytes(path, keep);
                }
            }
        }
        catch
        {
            // Logging must never throw — swallow.
        }
    }
}
