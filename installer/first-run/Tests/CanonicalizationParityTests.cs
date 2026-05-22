using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using AIBox.FirstRun.Services;
using Xunit;

namespace AIBox.FirstRun.Tests;

/// <summary>
/// Verifies that <see cref="ManifestParser.Canonicalize"/> produces bytes that
/// match Python's <c>manifest_canonical.canonical_bytes</c> byte-for-byte.
///
/// The EXPECTED_SAMPLE_MANIFEST_CANONICAL_SHA256 constant was computed by running:
///   cd C:\AIBox\aibox\installer\build
///   python -c "import json, hashlib, manifest_canonical; \
///     m = json.loads(open('../manifests/manifest-0.0.1.json').read()); \
///     b = manifest_canonical.canonical_bytes(m); \
///     print(hashlib.sha256(b).hexdigest())"
/// on 2026-05-19 against the manifest at manifests/manifest-0.0.1.json.
/// </summary>
public sealed class CanonicalizationParityTests
{
    // Computed by running Python's manifest_canonical.canonical_bytes on 2026-05-19.
    private const string EXPECTED_SAMPLE_MANIFEST_CANONICAL_SHA256 =
        "5aee48bc4d66de72f499943f5e25251501db34f8d56d95feaf4e85cdf5ec97cc";

    // Resolve the manifest file path relative to the test binary's output directory.
    // The .csproj copies the manifest via <None> CopyToOutputDirectory=PreserveNewest.
    private static string ManifestPath =>
        Path.Combine(
            Path.GetDirectoryName(typeof(CanonicalizationParityTests).Assembly.Location)!,
            "manifests",
            "manifest-0.0.1.json");

    // Helper: canonicalize a JSON value and return the canonical string for the "v" key.
    // Input is a C# string value (not JSON-encoded); we wrap it in {"v":<json>} and canonicalize.
    private static string CanonicalizeStringValue(string value)
    {
        // System.Text.Json.JsonSerializer.Serialize produces a valid JSON string token.
        var jsonToken = System.Text.Json.JsonSerializer.Serialize(value);
        var jsonIn = $"{{\"v\":{jsonToken}}}";
        var canonical = ManifestParser.Canonicalize(Encoding.UTF8.GetBytes(jsonIn));
        var result = Encoding.UTF8.GetString(canonical);
        // Strip {"v":...} wrapper and return only the value token.
        return result.Substring("{\"v\":".Length, result.Length - "{\"v\":".Length - 1);
    }

    // -------------------------------------------------------------------------
    // Test 1: Sample manifest SHA256 must match Python's output
    // -------------------------------------------------------------------------

    [Fact]
    public void Canonicalize_SampleManifest_Sha256MatchesPythonOutput()
    {
        Assert.True(File.Exists(ManifestPath),
            $"Manifest not found at expected output path: {ManifestPath}");
        var rawBytes = File.ReadAllBytes(ManifestPath);

        var canonical = ManifestParser.Canonicalize(rawBytes);
        var sha256 = SHA256.HashData(canonical);
        var hex = Convert.ToHexString(sha256).ToLowerInvariant();

        Assert.Equal(EXPECTED_SAMPLE_MANIFEST_CANONICAL_SHA256, hex);
    }

    // -------------------------------------------------------------------------
    // Test 2a: Named control character escapes
    // -------------------------------------------------------------------------

    [Theory]
    [InlineData("\b", "\"\\b\"")]
    [InlineData("\f", "\"\\f\"")]
    [InlineData("\n", "\"\\n\"")]
    [InlineData("\r", "\"\\r\"")]
    [InlineData("\t", "\"\\t\"")]
    // Structural characters that must be escaped
    [InlineData("\"", "\"\\\"\"")]
    [InlineData("\\", "\"\\\\\"")]
    // Combined: tab + backslash + quote
    [InlineData("\t\\\"", "\"\\t\\\\\\\"\"")]
    public void Canonicalize_NamedEscapeChars_MatchExpectedBytes(string input, string expectedJson)
    {
        Assert.Equal(expectedJson, CanonicalizeStringValue(input));
    }

    // -------------------------------------------------------------------------
    // Test 2b: Generic \uXXXX path for control characters without named escapes
    // -------------------------------------------------------------------------

    [Fact]
    public void Canonicalize_ControlChar_SOH_EncodesAsUnicodeEscape()
    {
        // U+0001 SOH — first control character above NUL, no named escape.
        // Python json.dumps('\x01', ensure_ascii=False) == '"\\u0001"'
        var input = "\u0001"; // C# unicode escape for U+0001 SOH
        Assert.Equal("\"\\u0001\"", CanonicalizeStringValue(input));
    }

    [Fact]
    public void Canonicalize_ControlChar_US_EncodesAsUnicodeEscape()
    {
        // U+001F US — highest control char below 0x20 that is not \b/\f/\n/\r/\t.
        // Python json.dumps('\x1f', ensure_ascii=False) == '"\\u001f"'
        var input = "\u001f"; // C# unicode escape for U+001F US
        Assert.Equal("\"\\u001f\"", CanonicalizeStringValue(input));
    }

    // -------------------------------------------------------------------------
    // Test 2c: Non-ASCII Unicode MUST pass through unescaped
    // -------------------------------------------------------------------------

    [Fact]
    public void Canonicalize_NonAsciiChar_SpanishN_PassesThroughUnescaped()
    {
        // Python ensure_ascii=False: 'n with tilde' passes through.
        Assert.Equal("\"ñ\"", CanonicalizeStringValue("ñ")); // ñ
    }

    [Fact]
    public void Canonicalize_NonAsciiChar_Emoji_PassesThroughUnescaped()
    {
        // U+1F600 GRINNING FACE emoji — Python ensure_ascii=False does NOT escape it.
        var emoji = "\U0001F600"; // C# surrogate pair for U+1F600
        var result = CanonicalizeStringValue(emoji);
        // Result must start and end with quotes and contain the emoji directly, not \uXXXX.
        Assert.StartsWith("\"", result);
        Assert.EndsWith("\"", result);
        Assert.DoesNotContain("\\u", result, StringComparison.Ordinal);
    }

    [Fact]
    public void Canonicalize_NonAsciiChar_U2028LineSeparator_PassesThroughUnescaped()
    {
        // U+2028 LINE SEPARATOR — not a JSON structural character; above 0x20.
        // Python ensure_ascii=False does NOT escape it, so C# must not either.
        var input = "\u2028"; // C# unicode escape for U+2028 LINE SEPARATOR
        var result = CanonicalizeStringValue(input);
        Assert.DoesNotContain("\\u", result, StringComparison.Ordinal);
        // The result must contain the actual U+2028 byte sequence (E2 80 A8 in UTF-8).
        var bytes = Encoding.UTF8.GetBytes(result);
        Assert.Contains((byte)0xE2, bytes);
    }

    // -------------------------------------------------------------------------
    // Test 3: Integer vs float canonicalization (Item 36 parity fix)
    // -------------------------------------------------------------------------

    [Fact]
    public void Canonicalize_IntegerValue_EmitsNoDecimalPoint()
    {
        var rawBytes = Encoding.UTF8.GetBytes("{\"a\": 5}");
        var canonical = ManifestParser.Canonicalize(rawBytes);
        Assert.Equal("{\"a\":5}", Encoding.UTF8.GetString(canonical));
    }

    [Fact]
    public void Canonicalize_FloatValue_PreservesDecimalPoint()
    {
        // Python json.dumps({"a": 5.0}) => '{"a": 5.0}' (canonical: '{"a":5.0}')
        // The C# canonicalizer must not truncate the decimal part.
        var rawBytes = Encoding.UTF8.GetBytes("{\"a\": 5.0}");
        var canonical = ManifestParser.Canonicalize(rawBytes);
        Assert.Equal("{\"a\":5.0}", Encoding.UTF8.GetString(canonical));
    }

    // -------------------------------------------------------------------------
    // Test 4: Sorted keys at every depth
    // -------------------------------------------------------------------------

    [Fact]
    public void Canonicalize_SortsKeysAtEveryDepth()
    {
        var rawBytes = Encoding.UTF8.GetBytes("{\"b\": 1, \"a\": {\"d\": 2, \"c\": 1}}");
        var canonical = ManifestParser.Canonicalize(rawBytes);
        Assert.Equal("{\"a\":{\"c\":1,\"d\":2},\"b\":1}", Encoding.UTF8.GetString(canonical));
    }

    // -------------------------------------------------------------------------
    // Test 5: Round-trip — canonicalize is idempotent
    // -------------------------------------------------------------------------

    [Fact]
    public void Canonicalize_IsIdempotent()
    {
        var rawBytes = Encoding.UTF8.GetBytes("{\"z\": 3, \"a\": 1, \"m\": [2, 1]}");
        var first = ManifestParser.Canonicalize(rawBytes);
        var second = ManifestParser.Canonicalize(first);
        Assert.Equal(first, second);
    }

    // -------------------------------------------------------------------------
    // Test 6: Output is UTF-8 with no BOM
    // -------------------------------------------------------------------------

    [Fact]
    public void Canonicalize_Output_HasNoUtf8Bom()
    {
        var rawBytes = Encoding.UTF8.GetBytes("{\"a\": 1}");
        var canonical = ManifestParser.Canonicalize(rawBytes);

        // UTF-8 BOM is EF BB BF.
        Assert.False(
            canonical.Length >= 3 && canonical[0] == 0xEF && canonical[1] == 0xBB && canonical[2] == 0xBF,
            "Canonical output must not begin with a UTF-8 BOM.");
    }
}
