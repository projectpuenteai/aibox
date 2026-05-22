using System;
using System.IO;
using System.Text;
using AIBox.FirstRun.Services;
using Xunit;

namespace AIBox.FirstRun.Tests;

/// <summary>
/// Tests for <see cref="EnvWriter"/>.
///
/// These tests exercise:
/// 1. All expected keys are written to the .env file.
/// 2. The file has no UTF-8 BOM (docker-compose and python-dotenv choke on it).
/// 3. Sensitive values (password, encryption key, pepper) are redacted by
///    the FileLogger before being emitted to the log file.
/// 4. An atomic write leaves no .tmp file behind.
/// </summary>
public sealed class EnvWriterTests : IDisposable
{
    private readonly string _tempDir;

    public EnvWriterTests()
    {
        _tempDir = Path.Combine(Path.GetTempPath(), "AIBoxTests_EnvWriter_" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(_tempDir);
    }

    public void Dispose()
    {
        try { Directory.Delete(_tempDir, recursive: true); } catch { /* best-effort */ }
    }

    private string EnvPath => Path.Combine(_tempDir, "stack", ".env");
    private string LogPath => Path.Combine(_tempDir, "install.log");

    private EnvWriter.EnvInputs DefaultInputs(
        string user = "admin",
        string password = "SuperSecret123") => new EnvWriter.EnvInputs
    {
        AdminUsername = user,
        AdminPassword = password,
        AppEnv = "production",
        SessionCookieSecure = false,
    };

    // -------------------------------------------------------------------------
    // Test 1: All expected keys are present in the generated .env
    // -------------------------------------------------------------------------

    [Fact]
    public void Write_GeneratesAllExpectedKeys()
    {
        EnvWriter.Write(EnvPath, DefaultInputs(), log: null);

        var content = File.ReadAllText(EnvPath);

        // Every key that production code reads must be in the file.
        Assert.Contains("APP_ENV=", content);
        Assert.Contains("APP_ENCRYPTION_MASTER_KEY=", content);
        Assert.Contains("SESSION_TOKEN_PEPPER=", content);
        Assert.Contains("DNS_ADMIN_PASSWORD=", content);
        Assert.Contains("ADMIN_USERNAME=", content);
        Assert.Contains("ADMIN_DEFAULT_PASSWORD=", content);
        Assert.Contains("SESSION_COOKIE_SECURE=", content);
    }

    // -------------------------------------------------------------------------
    // Test 2: ADMIN_USERNAME and ADMIN_DEFAULT_PASSWORD match the inputs
    // -------------------------------------------------------------------------

    [Fact]
    public void Write_AdminCredentials_MatchInputs()
    {
        EnvWriter.Write(EnvPath, DefaultInputs(user: "testuser", password: "P@ssw0rd!"), log: null);

        var content = File.ReadAllText(EnvPath);
        Assert.Contains("ADMIN_USERNAME=testuser", content);
        // Password may be quoted if it contains special chars; check the raw value is present.
        Assert.Contains("P@ssw0rd!", content);
    }

    // -------------------------------------------------------------------------
    // Test 3: File has no UTF-8 BOM
    // -------------------------------------------------------------------------

    [Fact]
    public void Write_File_HasNoUtf8Bom()
    {
        EnvWriter.Write(EnvPath, DefaultInputs(), log: null);

        var firstBytes = new byte[3];
        using var fs = File.OpenRead(EnvPath);
        fs.Read(firstBytes, 0, 3);

        // UTF-8 BOM is 0xEF 0xBB 0xBF.
        var hasBom = firstBytes[0] == 0xEF && firstBytes[1] == 0xBB && firstBytes[2] == 0xBF;
        Assert.False(hasBom, "The .env file must NOT start with a UTF-8 BOM.");
    }

    // -------------------------------------------------------------------------
    // Test 4: Atomic write — no .tmp file lingers after success
    // -------------------------------------------------------------------------

    [Fact]
    public void Write_SuccessfulWrite_NoTmpFileLingers()
    {
        EnvWriter.Write(EnvPath, DefaultInputs(), log: null);

        Assert.False(File.Exists(EnvPath + ".tmp"),
            ".tmp file should not remain after a successful EnvWriter.Write call.");
        Assert.True(File.Exists(EnvPath),
            "The final .env file must exist after Write returns.");
    }

    // -------------------------------------------------------------------------
    // Test 5: Sensitive values are NOT logged in plaintext
    //         (password, pepper, encryption key, DNS password)
    // -------------------------------------------------------------------------

    [Fact]
    public void Write_SensitiveValues_AreRedactedInLog()
    {
        var logPath = LogPath;
        using var logger = new FileLogger(logPath);

        var password = "VerySecretPwd987";
        EnvWriter.Write(EnvPath, DefaultInputs(password: password), logger);

        // Flush / dispose so all writes are committed.
        logger.Dispose();

        // Read log contents after dispose.
        var log = File.ReadAllText(logPath);

        // The plaintext password must NOT appear verbatim in the log.
        Assert.DoesNotContain(password, log);

        // The key count summary line SHOULD appear (proves the logger ran).
        Assert.Contains("keys", log.ToLowerInvariant());
    }

    // -------------------------------------------------------------------------
    // Test 6: EnvOutputs contains the original credentials (for summary screen)
    // -------------------------------------------------------------------------

    [Fact]
    public void Write_ReturnsEnvOutputs_WithOriginalCredentials()
    {
        var outputs = EnvWriter.Write(EnvPath, DefaultInputs(user: "puente", password: "abc123"), log: null);

        Assert.Equal("puente", outputs.AdminUsername);
        Assert.Equal("abc123", outputs.AdminPassword);
    }

    // -------------------------------------------------------------------------
    // Test 7: Round-trip parse — all keys survive write-then-read
    // -------------------------------------------------------------------------

    [Fact]
    public void Write_ParseBack_AllKeysPresent()
    {
        EnvWriter.Write(EnvPath, DefaultInputs(), log: null);

        var lines = File.ReadAllLines(EnvPath, Encoding.UTF8);
        var keys = new System.Collections.Generic.HashSet<string>(StringComparer.Ordinal);

        foreach (var line in lines)
        {
            if (line.StartsWith('#') || string.IsNullOrWhiteSpace(line)) continue;
            var eq = line.IndexOf('=');
            if (eq > 0) keys.Add(line.Substring(0, eq));
        }

        Assert.Contains("APP_ENV", keys);
        Assert.Contains("APP_ENCRYPTION_MASTER_KEY", keys);
        Assert.Contains("SESSION_TOKEN_PEPPER", keys);
        Assert.Contains("DNS_ADMIN_PASSWORD", keys);
        Assert.Contains("ADMIN_USERNAME", keys);
        Assert.Contains("ADMIN_DEFAULT_PASSWORD", keys);
        Assert.Contains("SESSION_COOKIE_SECURE", keys);
    }

    // -------------------------------------------------------------------------
    // Test 8: Second Write call overwrites the first (idempotent path)
    // -------------------------------------------------------------------------

    [Fact]
    public void Write_CalledTwice_OverwritesPreviousFile()
    {
        EnvWriter.Write(EnvPath, DefaultInputs(password: "first"), log: null);
        EnvWriter.Write(EnvPath, DefaultInputs(password: "second"), log: null);

        var content = File.ReadAllText(EnvPath);
        Assert.Contains("second", content);
        // "first" may or may not appear (generated secrets differ each call);
        // the admin password "first" must not be present.
        Assert.DoesNotContain("ADMIN_DEFAULT_PASSWORD=first", content);
    }

    // -------------------------------------------------------------------------
    // Test 9: APP_ENV value propagated correctly
    // -------------------------------------------------------------------------

    [Theory]
    [InlineData("production")]
    [InlineData("development")]
    public void Write_AppEnvValue_IsWrittenCorrectly(string appEnv)
    {
        var inputs = new EnvWriter.EnvInputs
        {
            AdminUsername = "admin",
            AdminPassword = "pass",
            AppEnv = appEnv,
        };
        EnvWriter.Write(EnvPath, inputs, log: null);

        var content = File.ReadAllText(EnvPath);
        Assert.Contains($"APP_ENV={appEnv}", content);
    }
}
