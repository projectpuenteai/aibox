using System;
using System.IO;
using System.Text;
using AIBox.FirstRun.Services;
using Xunit;

namespace AIBox.FirstRun.Tests;

/// <summary>
/// Structural and negative tests for <see cref="ManifestParser"/>.
///
/// NOTE: The sample manifest at manifests/manifest-0.0.1.json intentionally
/// uses "revision": "main" for its HF items (it is a dev/smoke-test fixture,
/// not a signed production manifest).  Because ManifestParser.Parse enforces
/// Item 34 (revision must be 40-hex SHA), parsing that file correctly throws.
/// These tests use hand-crafted inline JSON so every scenario can be exercised
/// independently of the on-disk fixture.
/// </summary>
public sealed class ManifestParserTests
{
    // -------------------------------------------------------------------------
    // Helper
    // -------------------------------------------------------------------------

    private static Manifest ParseJson(string json) =>
        ManifestParser.Parse(Encoding.UTF8.GetBytes(json));

    // A valid 40-char hex SHA to satisfy Item 34 in HF items.
    private const string ValidSha = "bb5d59e06d9551d752d08b292a50eb208b07ab1f";

    // -------------------------------------------------------------------------
    // Minimal valid manifest used as a baseline
    // -------------------------------------------------------------------------

    private const string MinimalManifest = """
        {
          "schema_version": 2,
          "release": "0.0.1",
          "min_installer_version": "0.0.1",
          "built_at": "2026-05-19T00:00:00Z",
          "notes": "test",
          "items": [
            {
              "id": "r2-item",
              "source": "r2",
              "url": "https://cdn.example.invalid/file.tar.zst",
              "target": "payload/file.tar.zst",
              "size_bytes": 1024,
              "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
            },
            {
              "id": "hf-single",
              "source": "huggingface",
              "repo": "org/model",
              "revision": "bb5d59e06d9551d752d08b292a50eb208b07ab1f",
              "path_in_repo": "config.json",
              "target": "models/config.json",
              "size_bytes": 900,
              "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
            },
            {
              "id": "hf-multi",
              "source": "huggingface",
              "repo": "org/model",
              "revision": "bb5d59e06d9551d752d08b292a50eb208b07ab1f",
              "include": ["*.json"],
              "target_dir": "models/multi/",
              "size_bytes_total": 2048,
              "files": [
                {
                  "path": "config.json",
                  "size_bytes": 0,
                  "sha256": ""
                }
              ]
            },
            {
              "id": "kiwix-item",
              "source": "kiwix",
              "catalog_query": { "name": "wikipedia_en_mini", "date": "any" },
              "fallback_url": "https://download.kiwix.org/zim/test.zim",
              "sha256_url": "https://download.kiwix.org/zim/test.zim.sha256",
              "target": "kiwix/test.zim",
              "size_bytes": 1000000
            },
            {
              "id": "kolibri-item",
              "source": "kolibri_channel",
              "studio_base_url": "https://studio.learningequality.org",
              "channel_id": "c1f2b7e6ac9f56a2bb44fa7a48b66dce",
              "approx_size_bytes": 0
            }
          ]
        }
        """;

    // -------------------------------------------------------------------------
    // Test 1: Parse valid manifest — schema / top-level fields
    // -------------------------------------------------------------------------

    [Fact]
    public void Parse_ValidManifest_TopLevelFieldsAreCorrect()
    {
        var manifest = ParseJson(MinimalManifest);

        Assert.Equal(2, manifest.SchemaVersion);
        Assert.Equal("0.0.1", manifest.Release);
        Assert.Equal("0.0.1", manifest.MinInstallerVersion);
        Assert.Equal("2026-05-19T00:00:00Z", manifest.BuiltAtUtc);
        Assert.Equal("test", manifest.Notes);
    }

    // -------------------------------------------------------------------------
    // Test 2: Parse valid manifest — item count and types
    // -------------------------------------------------------------------------

    [Fact]
    public void Parse_ValidManifest_ItemCountAndTypesAreCorrect()
    {
        var manifest = ParseJson(MinimalManifest);

        Assert.Equal(5, manifest.Items.Count);
        Assert.IsType<R2Item>(manifest.Items[0]);
        Assert.IsType<HuggingFaceItem>(manifest.Items[1]);
        Assert.IsType<HuggingFaceItem>(manifest.Items[2]);
        Assert.IsType<KiwixItem>(manifest.Items[3]);
        Assert.IsType<KolibriChannelItem>(manifest.Items[4]);
    }

    // -------------------------------------------------------------------------
    // Test 3: R2 item fields
    // -------------------------------------------------------------------------

    [Fact]
    public void Parse_R2Item_FieldsAreCorrect()
    {
        var manifest = ParseJson(MinimalManifest);
        var item = Assert.IsType<R2Item>(manifest.Items[0]);

        Assert.Equal("r2-item", item.Id);
        Assert.Equal("r2", item.Source);
        Assert.Equal("https://cdn.example.invalid/file.tar.zst", item.Url);
        Assert.Equal("payload/file.tar.zst", item.Target);
        Assert.Equal(1024L, item.SizeBytes);
        Assert.Equal("0000000000000000000000000000000000000000000000000000000000000000", item.Sha256);
    }

    // -------------------------------------------------------------------------
    // Test 4: HF single-file item fields
    // -------------------------------------------------------------------------

    [Fact]
    public void Parse_HfSingleFileItem_FieldsAreCorrect()
    {
        var manifest = ParseJson(MinimalManifest);
        var item = Assert.IsType<HuggingFaceItem>(manifest.Items[1]);

        Assert.Equal("hf-single", item.Id);
        Assert.Equal("huggingface", item.Source);
        Assert.Equal("org/model", item.Repo);
        Assert.Equal(ValidSha, item.Revision);
        Assert.Equal("config.json", item.PathInRepo);
        Assert.Equal("models/config.json", item.Target);
        Assert.Equal(900L, item.SizeBytes);
        Assert.False(item.IsMultiFile);
    }

    // -------------------------------------------------------------------------
    // Test 5: HF multi-file item — files array parses correctly
    // -------------------------------------------------------------------------

    [Fact]
    public void Parse_HfMultiFileItem_FilesArrayParsedCorrectly()
    {
        var manifest = ParseJson(MinimalManifest);
        var item = Assert.IsType<HuggingFaceItem>(manifest.Items[2]);

        Assert.Equal("hf-multi", item.Id);
        Assert.True(item.IsMultiFile);
        Assert.NotNull(item.Files);
        Assert.Single(item.Files!);
        Assert.Equal("config.json", item.Files![0].Path);
        Assert.Equal(0L, item.Files[0].SizeBytes);
        Assert.Equal("", item.Files[0].Sha256);
        Assert.NotNull(item.Include);
        Assert.Single(item.Include!);
        Assert.Equal("*.json", item.Include![0]);
    }

    // -------------------------------------------------------------------------
    // Test 6: Kiwix item with catalog_query
    // -------------------------------------------------------------------------

    [Fact]
    public void Parse_KiwixItem_CatalogQueryParsedCorrectly()
    {
        var manifest = ParseJson(MinimalManifest);
        var item = Assert.IsType<KiwixItem>(manifest.Items[3]);

        Assert.Equal("kiwix-item", item.Id);
        Assert.NotNull(item.CatalogQuery);
        Assert.Equal("wikipedia_en_mini", item.CatalogQuery!.Name);
        Assert.Equal("any", item.CatalogQuery.Date);
        Assert.Equal("kiwix/test.zim", item.Target);
    }

    // -------------------------------------------------------------------------
    // Test 7: Kolibri item
    // -------------------------------------------------------------------------

    [Fact]
    public void Parse_KolibriChannelItem_FieldsAreCorrect()
    {
        var manifest = ParseJson(MinimalManifest);
        var item = Assert.IsType<KolibriChannelItem>(manifest.Items[4]);

        Assert.Equal("kolibri-item", item.Id);
        Assert.Equal("kolibri_channel", item.Source);
        Assert.Equal("c1f2b7e6ac9f56a2bb44fa7a48b66dce", item.ChannelId);
        Assert.Equal("https://studio.learningequality.org", item.StudioBaseUrl);
    }

    // -------------------------------------------------------------------------
    // Negative test: schema_version != 2 throws InvalidDataException
    // -------------------------------------------------------------------------

    [Fact]
    public void Parse_SchemaVersion3_ThrowsInvalidDataException()
    {
        var json = MinimalManifest.Replace("\"schema_version\": 2", "\"schema_version\": 3");
        Assert.Throws<InvalidDataException>(() => ParseJson(json));
    }

    [Fact]
    public void Parse_SchemaVersion1_ThrowsInvalidDataException()
    {
        var json = MinimalManifest.Replace("\"schema_version\": 2", "\"schema_version\": 1");
        Assert.Throws<InvalidDataException>(() => ParseJson(json));
    }

    // -------------------------------------------------------------------------
    // Negative test: unknown source throws InvalidDataException
    // -------------------------------------------------------------------------

    [Fact]
    public void Parse_UnknownSource_ThrowsInvalidDataException()
    {
        var json = """
            {
              "schema_version": 2,
              "release": "0.0.1",
              "min_installer_version": "0.0.1",
              "built_at": "2026-05-19T00:00:00Z",
              "notes": "",
              "items": [
                {
                  "id": "bad-item",
                  "source": "dropbox",
                  "url": "https://example.com/file.zip",
                  "size_bytes": 0
                }
              ]
            }
            """;
        Assert.Throws<InvalidDataException>(() => ParseJson(json));
    }

    // -------------------------------------------------------------------------
    // Negative test (Item 34): HF item with revision "main" throws
    // -------------------------------------------------------------------------

    [Fact]
    public void Parse_HfItemWithRevisionMain_ThrowsInvalidDataException()
    {
        var json = """
            {
              "schema_version": 2,
              "release": "0.0.1",
              "min_installer_version": "0.0.1",
              "built_at": "2026-05-19T00:00:00Z",
              "notes": "",
              "items": [
                {
                  "id": "hf-floating",
                  "source": "huggingface",
                  "repo": "org/model",
                  "revision": "main",
                  "path_in_repo": "config.json",
                  "target": "models/config.json",
                  "size_bytes": 0
                }
              ]
            }
            """;
        var ex = Assert.Throws<InvalidDataException>(() => ParseJson(json));
        Assert.Contains("main", ex.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Parse_HfItemWithShortRevision_ThrowsInvalidDataException()
    {
        // 39 chars — one short of the required 40.
        var json = $$"""
            {
              "schema_version": 2,
              "release": "0.0.1",
              "min_installer_version": "0.0.1",
              "built_at": "2026-05-19T00:00:00Z",
              "notes": "",
              "items": [
                {
                  "id": "hf-short-sha",
                  "source": "huggingface",
                  "repo": "org/model",
                  "revision": "bb5d59e06d9551d752d08b292a50eb208b07ab1",
                  "path_in_repo": "config.json",
                  "target": "models/config.json",
                  "size_bytes": 0
                }
              ]
            }
            """;
        Assert.Throws<InvalidDataException>(() => ParseJson(json));
    }

    // -------------------------------------------------------------------------
    // Item 34 — required by plan §12 / REVIEW_FINDINGS item 34
    // -------------------------------------------------------------------------

    /// <summary>
    /// A HF item whose revision is the floating ref "main" must be hard-rejected
    /// with an InvalidDataException whose message identifies both the item id
    /// and the bad revision value.
    /// </summary>
    [Fact]
    public void Parse_HfRevision_FloatingRefMain_ThrowsWithItemIdAndValue()
    {
        var json = """
            {
              "schema_version": 2,
              "release": "0.0.1",
              "min_installer_version": "0.0.1",
              "built_at": "2026-05-19T00:00:00Z",
              "notes": "",
              "items": [
                {
                  "id": "hf-bad-revision",
                  "source": "huggingface",
                  "repo": "org/model",
                  "revision": "main",
                  "path_in_repo": "config.json",
                  "target": "models/config.json",
                  "size_bytes": 0
                }
              ]
            }
            """;
        var ex = Assert.Throws<InvalidDataException>(() => ParseJson(json));
        Assert.Contains("hf-bad-revision", ex.Message, StringComparison.Ordinal);
        Assert.Contains("main", ex.Message, StringComparison.Ordinal);
    }

    /// <summary>
    /// A HF item whose revision is a valid 40-character lowercase hex commit SHA
    /// must parse successfully without throwing.
    /// </summary>
    [Fact]
    public void Parse_HfRevision_Valid40HexSha_ParsesSuccessfully()
    {
        var json = $$"""
            {
              "schema_version": 2,
              "release": "0.0.1",
              "min_installer_version": "0.0.1",
              "built_at": "2026-05-19T00:00:00Z",
              "notes": "",
              "items": [
                {
                  "id": "hf-pinned",
                  "source": "huggingface",
                  "repo": "org/model",
                  "revision": "{{ValidSha}}",
                  "path_in_repo": "config.json",
                  "target": "models/config.json",
                  "size_bytes": 0
                }
              ]
            }
            """;
        var manifest = ParseJson(json);
        Assert.Single(manifest.Items);
        var item = Assert.IsType<HuggingFaceItem>(manifest.Items[0]);
        Assert.Equal(ValidSha, item.Revision);
        Assert.Equal("hf-pinned", item.Id);
    }

    // -------------------------------------------------------------------------
    // Negative test: manifest with no items array throws
    // -------------------------------------------------------------------------

    [Fact]
    public void Parse_MissingItemsArray_ThrowsInvalidDataException()
    {
        var json = """
            {
              "schema_version": 2,
              "release": "0.0.1",
              "min_installer_version": "0.0.1",
              "built_at": "2026-05-19T00:00:00Z",
              "notes": ""
            }
            """;
        Assert.Throws<InvalidDataException>(() => ParseJson(json));
    }

    // -------------------------------------------------------------------------
    // Negative test: item missing id throws
    // -------------------------------------------------------------------------

    [Fact]
    public void Parse_ItemMissingId_ThrowsInvalidDataException()
    {
        var json = """
            {
              "schema_version": 2,
              "release": "0.0.1",
              "min_installer_version": "0.0.1",
              "built_at": "2026-05-19T00:00:00Z",
              "notes": "",
              "items": [
                {
                  "source": "r2",
                  "url": "https://cdn.example.invalid/file.tar.zst",
                  "target": "payload/file.tar.zst",
                  "size_bytes": 0,
                  "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
                }
              ]
            }
            """;
        Assert.Throws<InvalidDataException>(() => ParseJson(json));
    }

    // -------------------------------------------------------------------------
    // Positive: hf-multi-file item with sha256_manifest and non-zero files
    // must have per-file SHA256 (Item 26 validation) — valid case passes
    // -------------------------------------------------------------------------

    [Fact]
    public void Parse_HfMultiFile_WithSha256Manifest_AllFilesHaveSha_Passes()
    {
        var json = """
            {
              "schema_version": 2,
              "release": "0.0.1",
              "min_installer_version": "0.0.1",
              "built_at": "2026-05-19T00:00:00Z",
              "notes": "",
              "items": [
                {
                  "id": "hf-multi-ok",
                  "source": "huggingface",
                  "repo": "org/model",
                  "revision": "bb5d59e06d9551d752d08b292a50eb208b07ab1f",
                  "target_dir": "models/multi/",
                  "size_bytes_total": 100,
                  "sha256_manifest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                  "files": [
                    {
                      "path": "model.bin",
                      "size_bytes": 100,
                      "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                    }
                  ]
                }
              ]
            }
            """;
        var manifest = ParseJson(json);
        Assert.Single(manifest.Items);
        var item = Assert.IsType<HuggingFaceItem>(manifest.Items[0]);
        Assert.NotNull(item.Files);
        Assert.Single(item.Files!);
    }

    // -------------------------------------------------------------------------
    // Negative (Item 26): HF multi-file with sha256_manifest but missing
    // per-file SHA for a non-zero file throws
    // -------------------------------------------------------------------------

    [Fact]
    public void Parse_HfMultiFile_WithSha256Manifest_FileMissingSha_Throws()
    {
        var json = """
            {
              "schema_version": 2,
              "release": "0.0.1",
              "min_installer_version": "0.0.1",
              "built_at": "2026-05-19T00:00:00Z",
              "notes": "",
              "items": [
                {
                  "id": "hf-multi-bad",
                  "source": "huggingface",
                  "repo": "org/model",
                  "revision": "bb5d59e06d9551d752d08b292a50eb208b07ab1f",
                  "target_dir": "models/multi/",
                  "size_bytes_total": 100,
                  "sha256_manifest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                  "files": [
                    {
                      "path": "model.bin",
                      "size_bytes": 100,
                      "sha256": ""
                    }
                  ]
                }
              ]
            }
            """;
        Assert.Throws<InvalidDataException>(() => ParseJson(json));
    }
}
