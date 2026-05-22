using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using AIBox.FirstRun.Services;
using Xunit;

namespace AIBox.FirstRun.Tests;

/// <summary>
/// Tests for <see cref="StateStore"/> and <see cref="InstallState"/> serialisation.
/// All tests use IDisposable to clean up temp files.
/// </summary>
public sealed class StateStoreTests : IDisposable
{
    private readonly string _tempDir;

    public StateStoreTests()
    {
        _tempDir = Path.Combine(Path.GetTempPath(), "AIBoxTests_StateStore_" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(_tempDir);
    }

    public void Dispose()
    {
        try { Directory.Delete(_tempDir, recursive: true); } catch { /* best-effort */ }
    }

    private string TempPath(string name = "state.json") =>
        Path.Combine(_tempDir, name);

    // -------------------------------------------------------------------------
    // Test 1: Round-trip — Save then Load restores all fields
    // -------------------------------------------------------------------------

    [Fact]
    public void SaveAndLoad_RoundTrip_AllFieldsArePreserved()
    {
        var path = TempPath();
        var original = new InstallState
        {
            SchemaVersion = 1,
            Version = "0.0.1",
            InstallRoot = @"C:\Program Files\AIBox",
            DataRoot = @"C:\ProgramData\AIBox",
            PhaseAComplete = true,
            PhaseBComplete = false,
            PhaseCComplete = false,
            PhaseCStep = "smoke-test",
            LastSavedAtUtc = "2026-05-19T12:34:56.0000000Z",
            Items = new Dictionary<string, ItemState>
            {
                ["r2-test"] = new ItemState
                {
                    Status = "done",
                    BytesDownloaded = 1024,
                    BytesTotal = 1024,
                    Sha256 = "deadbeef",
                    RetryCount = 1,
                    LastError = "",
                    SourceCursor = new Dictionary<string, string> { ["offset"] = "0" },
                },
            },
        };

        StateStore.Save(path, original);
        var loaded = StateStore.Load(path);

        Assert.Equal(original.SchemaVersion, loaded.SchemaVersion);
        Assert.Equal(original.Version, loaded.Version);
        Assert.Equal(original.InstallRoot, loaded.InstallRoot);
        Assert.Equal(original.DataRoot, loaded.DataRoot);
        Assert.True(loaded.PhaseAComplete);
        Assert.False(loaded.PhaseBComplete);
        Assert.Equal("smoke-test", loaded.PhaseCStep);
        Assert.Equal(original.LastSavedAtUtc, loaded.LastSavedAtUtc);

        Assert.Single(loaded.Items);
        var item = loaded.Items["r2-test"];
        Assert.Equal("done", item.Status);
        Assert.Equal(1024L, item.BytesDownloaded);
        Assert.Equal(1024L, item.BytesTotal);
        Assert.Equal("deadbeef", item.Sha256);
        Assert.Equal(1, item.RetryCount);
        Assert.Equal("0", item.SourceCursor["offset"]);
    }

    // -------------------------------------------------------------------------
    // Test 2: Load from non-existent path returns fresh InstallState
    // -------------------------------------------------------------------------

    [Fact]
    public void Load_NonExistentFile_ReturnsFreshInstallState()
    {
        var path = TempPath("does-not-exist.json");
        var state = StateStore.Load(path);

        Assert.NotNull(state);
        Assert.Equal(1, state.SchemaVersion);
        Assert.Equal("0.0.0", state.Version);
        Assert.Empty(state.Items);
    }

    // -------------------------------------------------------------------------
    // Test 3: Corrupt JSON — Load must not throw; returns fresh state;
    //         renames corrupt file with .corrupt-<ts> suffix
    // -------------------------------------------------------------------------

    [Fact]
    public void Load_CorruptJson_ReturnsFreshStateAndRenamesFile()
    {
        var path = TempPath("corrupt.json");
        File.WriteAllText(path, "this is not valid { json !!!", Encoding.UTF8);

        // Must not throw.
        var state = StateStore.Load(path);

        Assert.NotNull(state);
        Assert.Equal("0.0.0", state.Version);

        // Original file must no longer exist at that path.
        Assert.False(File.Exists(path), "Original corrupt file should have been renamed away.");

        // A .corrupt-* file must exist in the same directory.
        var corruptFiles = Directory.GetFiles(_tempDir, "corrupt.json.corrupt-*");
        Assert.Single(corruptFiles);
    }

    // -------------------------------------------------------------------------
    // Test 4: Load empty JSON file returns fresh state (not throw)
    // -------------------------------------------------------------------------

    [Fact]
    public void Load_EmptyFile_ReturnsFreshInstallState()
    {
        var path = TempPath("empty.json");
        File.WriteAllText(path, "", Encoding.UTF8);

        var state = StateStore.Load(path);
        Assert.NotNull(state);
        Assert.Equal("0.0.0", state.Version);
    }

    // -------------------------------------------------------------------------
    // Test 5: Atomic write — .tmp file must not linger after a successful Save
    // -------------------------------------------------------------------------

    [Fact]
    public void Save_SuccessfulWrite_NoTmpFileLingers()
    {
        var path = TempPath("atomic.json");
        var state = new InstallState { Version = "0.0.1" };

        StateStore.Save(path, state);

        Assert.False(File.Exists(path + ".tmp"),
            ".tmp file should not exist after a successful Save");
        Assert.True(File.Exists(path),
            "Final state file should exist after a successful Save");
    }

    // -------------------------------------------------------------------------
    // Test 6: Multiple saves — each overwrites the previous; file is readable
    // -------------------------------------------------------------------------

    [Fact]
    public void Save_MultipleSaves_LastValueWins()
    {
        var path = TempPath("multi.json");

        StateStore.Save(path, new InstallState { Version = "0.0.1", PhaseAComplete = false });
        StateStore.Save(path, new InstallState { Version = "0.0.1", PhaseAComplete = true });

        var state = StateStore.Load(path);
        Assert.True(state.PhaseAComplete);
    }

    // -------------------------------------------------------------------------
    // Test 7: Touch() sets LastSavedAtUtc to a parseable UTC ISO-8601 string
    // -------------------------------------------------------------------------

    [Fact]
    public void Touch_SetsLastSavedAtUtcToValidIso8601()
    {
        var state = new InstallState();
        var before = DateTime.UtcNow;
        state.Touch();
        var after = DateTime.UtcNow;

        var parsed = DateTime.Parse(state.LastSavedAtUtc,
            System.Globalization.CultureInfo.InvariantCulture,
            System.Globalization.DateTimeStyles.RoundtripKind);

        Assert.True(parsed >= before.AddSeconds(-1));
        Assert.True(parsed <= after.AddSeconds(1));
    }

    // -------------------------------------------------------------------------
    // Test 8: Save / Load preserves snake_case key names (integration sanity)
    //         "Version" is serialized as "version" per [JsonPropertyName]
    // -------------------------------------------------------------------------

    [Fact]
    public void Save_ProducesSnakeCaseJson()
    {
        var path = TempPath("snake.json");
        StateStore.Save(path, new InstallState { Version = "1.2.3", PhaseAComplete = true });

        var raw = File.ReadAllText(path);
        // snake_case keys expected from JsonNamingPolicy.SnakeCaseLower
        Assert.Contains("\"version\"", raw);
        Assert.Contains("\"phase_a_complete\"", raw);
        Assert.Contains("\"install_root\"", raw);
    }
}
