using System;
using System.IO;
using System.Net.Http;
using System.Reflection;
using System.Security.Cryptography;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Resources;

namespace AIBox.FirstRun.Services;

/// <summary>
/// Fetches manifest-&lt;v&gt;.json + .sig from the primary R2 URL with a
/// fallback to GitHub releases, then verifies the ed25519 signature
/// against the embedded release public key. Only after verification do
/// we return a parsed <see cref="Manifest"/>.
/// </summary>
public sealed class ManifestClient
{
    private readonly InstallContext _ctx;
    private readonly HttpClient _http;
    private readonly FileLogger _log;

    public ManifestClient(InstallContext ctx, HttpClient http, FileLogger log)
    {
        _ctx = ctx;
        _http = http;
        _log = log;
    }

    public async Task<Manifest> FetchAndVerifyAsync(CancellationToken ct)
    {
        byte[] manifestBytes;
        byte[] sigBytes;

        // Prefer the bundled manifest dropped by Inno into {InstallRoot}\manifest\.
        // This is the offline-first path — no network access needed for the manifest itself.
        // Falls through to URL fetch only if the bundled file is absent (e.g., when running
        // First Run from a dev checkout instead of an installed tree).
        if (File.Exists(_ctx.BundledManifestPath) && File.Exists(_ctx.BundledManifestSigPath))
        {
            _log.Info($"Loading bundled manifest from {_ctx.BundledManifestPath}");
            manifestBytes = await File.ReadAllBytesAsync(_ctx.BundledManifestPath, ct).ConfigureAwait(false);
            sigBytes      = await File.ReadAllBytesAsync(_ctx.BundledManifestSigPath, ct).ConfigureAwait(false);
        }
        else
        {
            // Refuse to even attempt a URL fetch if CI didn't rewrite ManifestBaseUrl
            // away from the .invalid sentinel. Without this, the user gets a confusing
            // TLS error on a non-existent domain instead of a clear configuration message.
            if (BuildConstants.ManifestBaseUrl.Contains(".invalid", StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException(
                    "Manifest URL is still the placeholder .invalid sentinel and no bundled manifest " +
                    "was found at " + _ctx.BundledManifestPath + ". " +
                    "This binary was built without CI rewriting BuildConstants.ManifestBaseUrl — " +
                    "it cannot be used to fetch a real release.");
            }

            (manifestBytes, sigBytes) = await FetchWithFallbackAsync(ct).ConfigureAwait(false);
        }

        var pubKey = LoadEmbeddedPublicKey();

        var canonical = ManifestParser.Canonicalize(manifestBytes);
        bool ok = Ed25519Verifier.Verify(pubKey, canonical, sigBytes);
        if (!ok)
        {
            _log.Error("Manifest signature did NOT verify against embedded public key.");
            throw new InvalidDataException(
                "Manifest signature verification failed. " +
                "Refusing to proceed — re-download AIBox-Setup.exe from the official release.");
        }
        _log.Info("Manifest signature verified.");

        var manifest = ManifestParser.Parse(manifestBytes);
        _log.Info($"Manifest release={manifest.Release}, items={manifest.Items.Count}, built_at={manifest.BuiltAtUtc}.");
        return manifest;
    }

    // Cap manifest downloads at 1 MB to prevent a malicious origin from exhausting
    // memory before the signature check fails. A signed manifest with 1000 items is
    // still well under 100 KB, so 1 MB is generous. The signature itself is exactly
    // 64 bytes (Ed25519 raw); cap at 4 KB to leave room for future embedding without
    // accepting megabyte-size junk.
    private const long MaxManifestBytes = 1L * 1024 * 1024;
    private const long MaxSignatureBytes = 4L * 1024;

    private async Task<(byte[] manifest, byte[] sig)> FetchWithFallbackAsync(CancellationToken ct)
    {
        var primary = _ctx.ManifestUrl;
        var sig = _ctx.ManifestSigUrl;

        try
        {
            _log.Info($"Fetching manifest from primary: {primary}");
            var m = await FetchWithMaxBytesAsync(_http, primary, MaxManifestBytes, ct).ConfigureAwait(false);
            var s = await FetchWithMaxBytesAsync(_http, sig, MaxSignatureBytes, ct).ConfigureAwait(false);
            return (m, s);
        }
        catch (Exception ex)
        {
            _log.Warn($"Primary manifest fetch failed ({ex.GetType().Name}): {ex.Message}. Falling back.");
        }

        var fallback = _ctx.ManifestFallbackUrl;
        var fallbackSig = new Uri(fallback.OriginalString + ".sig");
        _log.Info($"Fetching manifest from fallback: {fallback}");
        var fm = await FetchWithMaxBytesAsync(_http, fallback, MaxManifestBytes, ct).ConfigureAwait(false);
        var fs = await FetchWithMaxBytesAsync(_http, fallbackSig, MaxSignatureBytes, ct).ConfigureAwait(false);
        return (fm, fs);
    }

    /// <summary>
    /// Fetch from a URL with a strict byte limit. Rejects the response if
    /// cumulative bytes exceed maxBytes before reading completes.
    /// </summary>
    private static async Task<byte[]> FetchWithMaxBytesAsync(HttpClient http, Uri uri, long maxBytes, CancellationToken ct)
    {
        using var resp = await http.GetAsync(uri, HttpCompletionOption.ResponseHeadersRead, ct).ConfigureAwait(false);
        resp.EnsureSuccessStatusCode();

        using var stream = await resp.Content.ReadAsStreamAsync(ct).ConfigureAwait(false);
        using var ms = new MemoryStream();
        var buffer = new byte[8192];
        long totalRead = 0;
        int read;
        while ((read = await stream.ReadAsync(buffer, ct).ConfigureAwait(false)) > 0)
        {
            totalRead += read;
            if (totalRead > maxBytes)
                throw new InvalidDataException(
                    $"Manifest exceeded size limit of {maxBytes} bytes (received {totalRead} bytes); rejecting.");
            await ms.WriteAsync(buffer.AsMemory(0, read), ct).ConfigureAwait(false);
        }
        return ms.ToArray();
    }

    private static byte[] LoadEmbeddedPublicKey()
    {
        var uri = new Uri("pack://application:,,,/" + BuildConstants.ReleasePubKeyResource, UriKind.Absolute);
        StreamResourceInfo? info;
        try { info = Application.GetResourceStream(uri); }
        catch
        {
            // Fall back to embedded assembly resource (running tests outside the WPF host).
            return LoadFromAssembly();
        }
        if (info is null) return LoadFromAssembly();

        using var ms = new MemoryStream();
        info.Stream.CopyTo(ms);
        var raw = ms.ToArray();
        return NormalizeKey(raw);
    }

    private static byte[] LoadFromAssembly()
    {
        var asm = Assembly.GetExecutingAssembly();
        var resName = "AIBox.FirstRun.Resources.release-pubkey.ed25519";
        using var stream = asm.GetManifestResourceStream(resName)
            ?? throw new InvalidOperationException("Embedded release public key resource not found.");
        using var ms = new MemoryStream();
        stream.CopyTo(ms);
        return NormalizeKey(ms.ToArray());
    }

    private static byte[] NormalizeKey(byte[] raw)
    {
        // Accept either a 32-byte raw key or a base64-encoded form (with optional whitespace).
        if (raw.Length == 32) return raw;
        var text = System.Text.Encoding.UTF8.GetString(raw).Trim();
        try
        {
            var decoded = Convert.FromBase64String(text);
            if (decoded.Length == 32) return decoded;
        }
        catch (FormatException) { /* fall through */ }
        throw new InvalidDataException(
            $"Embedded release public key has unexpected size {raw.Length}; expected 32 raw bytes or base64.");
    }
}
