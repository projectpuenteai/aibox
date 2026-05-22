using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace AIBox.FirstRun.Services;

/// <summary>
/// In-memory representation of a parsed manifest (schema_version=2).
/// The trust boundary is enforced earlier in ManifestClient: by the
/// time you have a <see cref="Manifest"/>, the bytes it came from
/// have already been verified against the embedded release pubkey.
/// </summary>
public sealed class Manifest
{
    public int SchemaVersion { get; set; }
    public string Release { get; set; } = "";
    public string MinInstallerVersion { get; set; } = "";
    public string BuiltAtUtc { get; set; } = "";
    public string Notes { get; set; } = "";
    public List<ManifestItem> Items { get; set; } = new();
}

/// <summary>
/// One entry in <see cref="Manifest.Items"/>. We deserialize the union by
/// reading <c>source</c> first and then dispatching — all sources share
/// these top-level fields; the rest live on a per-source subclass.
/// </summary>
public abstract class ManifestItem
{
    public string Id { get; set; } = "";
    public string Source { get; set; } = "";
}

public sealed class R2Item : ManifestItem
{
    public string Url { get; set; } = "";
    public string Target { get; set; } = "";
    public string? ExtractTo { get; set; }
    public long SizeBytes { get; set; }
    public string Sha256 { get; set; } = "";
}

public sealed class HuggingFaceItem : ManifestItem
{
    public string Repo { get; set; } = "";
    public string Revision { get; set; } = "";
    public string? PathInRepo { get; set; }
    public string? Target { get; set; }
    public string? TargetDir { get; set; }
    public List<string>? Include { get; set; }
    public long SizeBytes { get; set; }
    public long SizeBytesTotal { get; set; }
    public string? Sha256 { get; set; }
    public string? Sha256Manifest { get; set; }
    public List<HfFileEntry>? Files { get; set; }
    public bool IsMultiFile => !string.IsNullOrEmpty(TargetDir);
}

public sealed class HfFileEntry
{
    public string Path { get; set; } = "";
    public long SizeBytes { get; set; }
    public string Sha256 { get; set; } = "";
}

public sealed class KiwixItem : ManifestItem
{
    public CatalogQuery? CatalogQuery { get; set; }
    public string? FallbackUrl { get; set; }
    public string? Sha256Url { get; set; }
    public string Target { get; set; } = "";
    public long SizeBytes { get; set; }
}

public sealed class CatalogQuery
{
    public string Name { get; set; } = "";
    public string Date { get; set; } = "any";
}

public sealed class KolibriChannelItem : ManifestItem
{
    public string StudioBaseUrl { get; set; } = "https://studio.learningequality.org";
    public string ChannelId { get; set; } = "";
    public List<string>? IncludeNodeIds { get; set; }
    public long ApproxSizeBytes { get; set; }
}

/// <summary>
/// Parses the canonical manifest JSON into a <see cref="Manifest"/>.
/// Hand-written rather than via JsonSerializer attributes because the
/// items array is a discriminated union keyed on the <c>source</c> field.
/// </summary>
public static class ManifestParser
{
    public static Manifest Parse(byte[] utf8Bytes)
    {
        var root = JsonNode.Parse(utf8Bytes)
                   ?? throw new InvalidDataException("Manifest is empty.");
        var obj = root.AsObject();

        var manifest = new Manifest
        {
            SchemaVersion = obj["schema_version"]?.GetValue<int>() ?? 0,
            Release = obj["release"]?.GetValue<string>() ?? "",
            MinInstallerVersion = obj["min_installer_version"]?.GetValue<string>() ?? "",
            BuiltAtUtc = obj["built_at"]?.GetValue<string>() ?? "",
            Notes = obj["notes"]?.GetValue<string>() ?? "",
        };

        if (manifest.SchemaVersion != 2)
            throw new InvalidDataException(
                $"Unsupported manifest schema_version {manifest.SchemaVersion}; this installer supports 2.");

        var items = obj["items"]?.AsArray()
                    ?? throw new InvalidDataException("Manifest has no items array.");
        foreach (var raw in items)
        {
            if (raw is not JsonObject item) continue;
            var src = item["source"]?.GetValue<string>() ?? "";
            ManifestItem parsed = src switch
            {
                "r2" => ParseR2(item),
                "huggingface" => ParseHuggingFace(item),
                "kiwix" => ParseKiwix(item),
                "kolibri_channel" => ParseKolibri(item),
                _ => throw new InvalidDataException($"Unknown manifest source '{src}'."),
            };
            parsed.Id = item["id"]?.GetValue<string>() ?? "";
            parsed.Source = src;
            if (string.IsNullOrWhiteSpace(parsed.Id))
                throw new InvalidDataException("Manifest item is missing an 'id'.");
            manifest.Items.Add(parsed);
        }

        if (manifest.Items.Count == 0)
            throw new InvalidDataException(
                "Manifest has no items — refusing to mark install complete on an empty payload.");

        return manifest;
    }

    private static R2Item ParseR2(JsonObject o) => new()
    {
        Url = o["url"]?.GetValue<string>() ?? "",
        Target = o["target"]?.GetValue<string>() ?? "",
        ExtractTo = o["extract_to"]?.GetValue<string>(),
        SizeBytes = o["size_bytes"]?.GetValue<long>() ?? 0,
        Sha256 = o["sha256"]?.GetValue<string>() ?? "",
    };

    private static HuggingFaceItem ParseHuggingFace(JsonObject o)
    {
        var revision = o["revision"]?.GetValue<string>() ?? "";
        var itemId = o["id"]?.GetValue<string>() ?? "(unknown)";

        // Item 34: Validate that revision is a 40-character lowercase hex commit SHA.
        // Empty strings, floating refs like "main"/"v1.0", and uppercase hex are all rejected.
        if (!IsValidHfRevision(revision))
            throw new InvalidDataException(
                $"HF item '{itemId}' has revision '{revision}' which is not a valid 40-character lowercase hex commit SHA. " +
                "Production manifests must pin to a specific commit SHA, not floating refs like 'main' or 'v1.0'.");

        var item = new HuggingFaceItem
        {
            Repo = o["repo"]?.GetValue<string>() ?? "",
            Revision = revision,
            PathInRepo = o["path_in_repo"]?.GetValue<string>(),
            Target = o["target"]?.GetValue<string>(),
            TargetDir = o["target_dir"]?.GetValue<string>(),
            SizeBytes = o["size_bytes"]?.GetValue<long>() ?? 0,
            SizeBytesTotal = o["size_bytes_total"]?.GetValue<long>() ?? 0,
            Sha256 = o["sha256"]?.GetValue<string>(),
            Sha256Manifest = o["sha256_manifest"]?.GetValue<string>(),
        };
        if (o["include"] is JsonArray inc)
            item.Include = inc.Select(x => x?.GetValue<string>() ?? "").Where(s => s.Length > 0).ToList();
        if (o["files"] is JsonArray files)
        {
            item.Files = files.OfType<JsonObject>().Select(f => new HfFileEntry
            {
                Path = f["path"]?.GetValue<string>() ?? "",
                SizeBytes = f["size_bytes"]?.GetValue<long>() ?? 0,
                Sha256 = f["sha256"]?.GetValue<string>() ?? "",
            }).ToList();

            // Item 26: For multi-file items with sha256_manifest, all files in the manifest
            // must have SHA256 set. Non-LFS files from HF may not have SHAs in the tree API,
            // but the build_manifest.py CI step is responsible for fetching and hashing them locally.
            // If the manifest is populated but a file has empty SHA, that's a manifest integrity error.
            if (!string.IsNullOrEmpty(item.Sha256Manifest))
            {
                foreach (var file in item.Files)
                {
                    if (file.SizeBytes > 0 && string.IsNullOrEmpty(file.Sha256))
                        throw new InvalidDataException(
                            $"HF item {o["id"]?.GetValue<string>() ?? "(unknown)"} has sha256_manifest set, " +
                            $"but file '{file.Path}' ({file.SizeBytes} bytes) has no SHA256. " +
                            "Build manifest must populate per-file SHAs for all non-zero-length files.");
                }
            }
        }
        return item;
    }

    /// <summary>
    /// Validate that a HF revision is exactly 40 lowercase hex characters (a commit SHA).
    /// Uppercase hex, empty strings, and floating refs like "main" are all rejected.
    /// </summary>
    private static bool IsValidHfRevision(string rev)
    {
        if (rev.Length != 40) return false;
        foreach (var c in rev)
            if (c < '0' || (c > '9' && c < 'a') || c > 'f')
                return false;
        return true;
    }

    private static KiwixItem ParseKiwix(JsonObject o)
    {
        var item = new KiwixItem
        {
            FallbackUrl = o["fallback_url"]?.GetValue<string>(),
            Sha256Url = o["sha256_url"]?.GetValue<string>(),
            Target = o["target"]?.GetValue<string>() ?? "",
            SizeBytes = o["size_bytes"]?.GetValue<long>() ?? 0,
        };
        if (o["catalog_query"] is JsonObject cq)
        {
            item.CatalogQuery = new CatalogQuery
            {
                Name = cq["name"]?.GetValue<string>() ?? "",
                Date = cq["date"]?.GetValue<string>() ?? "any",
            };
        }
        return item;
    }

    private static KolibriChannelItem ParseKolibri(JsonObject o)
    {
        var item = new KolibriChannelItem
        {
            StudioBaseUrl = o["studio_base_url"]?.GetValue<string>() ?? "https://studio.learningequality.org",
            ChannelId = o["channel_id"]?.GetValue<string>() ?? "",
            ApproxSizeBytes = o["approx_size_bytes"]?.GetValue<long>() ?? 0,
        };
        if (o["include_node_ids"] is JsonArray ids)
            item.IncludeNodeIds = ids.Select(x => x?.GetValue<string>() ?? "").Where(s => s.Length > 0).ToList();
        return item;
    }

    /// <summary>
    /// Canonicalize the manifest JSON for signature verification. MUST match
    /// build/manifest_canonical.py byte-for-byte:
    ///   UTF-8, sorted keys at every depth, compact separators, no escaping.
    /// </summary>
    public static byte[] Canonicalize(byte[] utf8Bytes)
    {
        var root = JsonNode.Parse(utf8Bytes)
                   ?? throw new InvalidDataException("Manifest is empty.");
        return Encoding.UTF8.GetBytes(CanonicalizeNode(root));
    }

    private static string CanonicalizeNode(JsonNode? node)
    {
        if (node is null) return "null";
        if (node is JsonValue v)
        {
            if (v.TryGetValue(out bool b)) return b ? "true" : "false";

            // Item 36: Handle numeric canonicalization to match Python json.dumps.
            // Check if the raw JSON text indicates a float (contains '.', 'e', or 'E').
            // If so, use double formatting; otherwise, parse as long (int-only).
            // Example:
            //   JSON source "5" -> parse as long, emit as "5"
            //   JSON source "5.0" -> has decimal point, emit as "5.0" (matching Python)
            //   JSON source "1e10" -> has exponent, emit as "1E+10" (using double format)
            try
            {
                var elem = v.GetValue<JsonElement>();
                var rawText = elem.GetRawText();

                // Check if this looks like a float in the source JSON.
                bool isFloatInSource = rawText.IndexOfAny(new[] { '.', 'e', 'E' }) >= 0;

                if (!isFloatInSource && v.TryGetValue(out long i))
                {
                    return i.ToString(System.Globalization.CultureInfo.InvariantCulture);
                }
            }
            catch { /* fall through to standard parsing */ }

            if (v.TryGetValue(out double d))
            {
                if (double.IsNaN(d) || double.IsInfinity(d))
                    throw new InvalidDataException("Manifest contains NaN/Infinity — refused.");
                var dstr = d.ToString("R", System.Globalization.CultureInfo.InvariantCulture);
                // Match Python json.dumps: floats always carry a fractional or exponent
                // marker. `5.0` must serialize as "5.0", not "5". `"R"` collapses
                // integer-valued doubles to "5", so re-append ".0" in that case.
                if (dstr.IndexOfAny(new[] { '.', 'e', 'E' }) < 0)
                    dstr += ".0";
                return dstr;
            }
            if (v.TryGetValue(out string? s) && s != null) return EncodeString(s);
            return v.ToJsonString(new JsonSerializerOptions { Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping });
        }
        if (node is JsonObject o)
        {
            var sb = new StringBuilder();
            sb.Append('{');
            bool first = true;
            foreach (var key in o.Select(p => p.Key).OrderBy(k => k, StringComparer.Ordinal))
            {
                if (!first) sb.Append(',');
                first = false;
                sb.Append(EncodeString(key));
                sb.Append(':');
                sb.Append(CanonicalizeNode(o[key]));
            }
            sb.Append('}');
            return sb.ToString();
        }
        if (node is JsonArray a)
        {
            var sb = new StringBuilder();
            sb.Append('[');
            bool first = true;
            foreach (var elem in a)
            {
                if (!first) sb.Append(',');
                first = false;
                sb.Append(CanonicalizeNode(elem));
            }
            sb.Append(']');
            return sb.ToString();
        }
        throw new InvalidDataException("Unknown JSON node type.");
    }

    private static string EncodeString(string s)
    {
        // Match Python json.dumps(ensure_ascii=False): only escape the structural set
        // and control chars. Non-ASCII unicode passes through.
        var sb = new StringBuilder(s.Length + 2);
        sb.Append('"');
        foreach (var c in s)
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\b': sb.Append("\\b"); break;
                case '\f': sb.Append("\\f"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 0x20)
                        sb.AppendFormat("\\u{0:x4}", (int)c);
                    else
                        sb.Append(c);
                    break;
            }
        }
        sb.Append('"');
        return sb.ToString();
    }
}
