using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace AIBox.FirstRun.Services;

/// <summary>
/// Read/write %ProgramData%\AIBox\install-state.json with atomic writes
/// (tmp -> fsync -> rename) so a crash mid-write can't leave a
/// half-serialized state file. Source of truth for resumability.
/// </summary>
public static class StateStore
{
    private static readonly object FileLock = new();
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        ReadCommentHandling = JsonCommentHandling.Skip,
        AllowTrailingCommas = true,
    };

    public static InstallState Load(string path) => LoadWithStatus(path).State;

    public sealed class LoadResult
    {
        public required InstallState State { get; init; }
        public required LoadStatus Status { get; init; }
        public string? CorruptBackupPath { get; init; }
    }

    public enum LoadStatus
    {
        /// <summary>File parsed cleanly into an InstallState.</summary>
        Ok,
        /// <summary>File did not exist — fresh install.</summary>
        Missing,
        /// <summary>File existed but was empty or whitespace-only.</summary>
        Empty,
        /// <summary>File existed but could not be parsed; backup written, fresh state returned.</summary>
        Corrupt,
    }

    public static LoadResult LoadWithStatus(string path)
    {
        lock (FileLock)
        {
            if (!File.Exists(path))
                return new LoadResult { State = new InstallState(), Status = LoadStatus.Missing };

            try
            {
                var json = File.ReadAllText(path);
                if (string.IsNullOrWhiteSpace(json))
                    return new LoadResult { State = new InstallState(), Status = LoadStatus.Empty };
                var state = JsonSerializer.Deserialize<InstallState>(json, JsonOpts) ?? new InstallState();
                return new LoadResult { State = state, Status = LoadStatus.Ok };
            }
            catch (JsonException)
            {
                // Corrupt state: rename out of the way and start fresh. We do
                // not delete because a developer may want to inspect it.
                var backup = path + $".corrupt-{DateTime.UtcNow:yyyyMMddHHmmss}";
                File.Move(path, backup);
                return new LoadResult
                {
                    State = new InstallState(),
                    Status = LoadStatus.Corrupt,
                    CorruptBackupPath = backup,
                };
            }
        }
    }

    public static void Save(string path, InstallState state)
    {
        lock (FileLock)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            var tmp = path + ".tmp";
            var json = JsonSerializer.Serialize(state, JsonOpts);
            File.WriteAllText(tmp, json);

            using (var fs = new FileStream(tmp, FileMode.Open, FileAccess.ReadWrite, FileShare.Read))
                fs.Flush(true);

            if (File.Exists(path))
            {
                File.Replace(tmp, path, null);
            }
            else
            {
                File.Move(tmp, path);
            }
        }
    }
}

public sealed class InstallState
{
    public int SchemaVersion { get; set; } = 1;

    [JsonPropertyName("version")]
    public string Version { get; set; } = "0.0.0";

    public string InstallRoot { get; set; } = "";
    public string DataRoot { get; set; } = "";

    public bool PhaseAComplete { get; set; }
    public bool PhaseBComplete { get; set; }
    public bool PhaseCComplete { get; set; }

    /// <summary>Sub-step within Phase C so we can resume after the smoke test fails, etc.</summary>
    public string PhaseCStep { get; set; } = "";

    /// <summary>Per-item download cursors keyed by manifest item id.</summary>
    public Dictionary<string, ItemState> Items { get; set; } = new();

    /// <summary>UTC timestamp of last save, useful for the >60s stale heuristic.</summary>
    public string LastSavedAtUtc { get; set; } = "";

    public void Touch()
    {
        LastSavedAtUtc = DateTime.UtcNow.ToString("o");
    }
}

public sealed class ItemState
{
    public string Status { get; set; } = "pending";  // pending|downloading|verifying|done|failed
    public long BytesDownloaded { get; set; }
    public long BytesTotal { get; set; }
    public string Sha256 { get; set; } = "";
    public int RetryCount { get; set; }
    public string LastError { get; set; } = "";
    public string LastErrorAtUtc { get; set; } = "";

    /// <summary>Source-specific resume cursor (HF file index, Kolibri import phase, etc).</summary>
    public Dictionary<string, string> SourceCursor { get; set; } = new();
}
