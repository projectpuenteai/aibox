# Release manifests

Each `manifest-<version>.json` is a signed routing document that tells
the First Run app where to fetch every artifact in a release. The
schema is locked at `schema_version: 2`.

## Why a manifest exists

The installer `.exe` ships once; the content it pulls evolves. A new
Wikipedia dump, a re-uploaded Chroma index, an updated Kolibri channel
— all of these are content-only updates that publish a new manifest
without rebuilding the installer.

The pointer file `latest.json` at `cdn.<ourdomain>/aibox/latest.json`
tells installers which manifest version to fetch:

```json
{
  "current_release": "1.0.0",
  "min_installer_version": "1.0.0",
  "manifest_url": "https://cdn.<ourdomain>/aibox/manifest-1.0.0.json"
}
```

If a manifest requires installer features the user's `.exe` lacks,
`min_installer_version` blocks it with a clear "download a new
setup.exe" message.

## Schema (`schema_version: 2`)

Top-level:

| Field                   | Type        | Required | Notes                          |
| ----------------------- | ----------- | -------- | ------------------------------ |
| `schema_version`        | int         | yes      | Must be `2`                    |
| `release`               | string      | yes      | Semantic version, e.g. `"1.0.0"` |
| `min_installer_version` | string      | yes      | Lowest setup.exe that can read it |
| `built_at`              | RFC 3339    | yes      | UTC build timestamp            |
| `items`                 | array       | yes      | Payload to fetch (see below)   |

### Item types

Each item has a `source` field that selects the fetcher. The other
fields per source:

#### `source: "huggingface"` — single file

```json
{
  "id": "llm-qwen2.5-7b-q4-part1",
  "source": "huggingface",
  "repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
  "revision": "bb5d59e06d9551d752d08b292a50eb208b07ab1f",
  "path_in_repo": "qwen2.5-7b-instruct-q4_0-00001-of-00002.gguf",
  "target": "models/llm/gguf/qwen2.5-7b-instruct-q4_0-00001-of-00002.gguf",
  "size_bytes": 3983228352,
  "sha256": "<full-file sha256>"
}
```

#### `source: "huggingface"` — multi-file (tree under a repo)

```json
{
  "id": "embed-bge-m3",
  "source": "huggingface",
  "repo": "BAAI/bge-m3",
  "revision": "5617a9f61b028005a4858fdac845db406aefb181",
  "include": ["*.json", "*.safetensors", "*.txt", "sentencepiece.bpe.model"],
  "target_dir": "models/embed-m3/",
  "size_bytes_total": 2270000000,
  "files": [
    {"path": "config.json", "size_bytes": 687, "sha256": "<hash>"},
    ...
  ],
  "sha256_manifest": "<sha256 of sorted '<path> <sha256>' lines>"
}
```

The `files` array enumerates every file the installer should fetch.
`sha256_manifest` is a meta-hash over the sorted list — protects
against a tampered HF tree response inserting a fake file.

#### `source: "kiwix"`

```json
{
  "id": "zim-en",
  "source": "kiwix",
  "catalog_query": {"name": "wikipedia_en_all_mini", "date": "2026-03"},
  "fallback_url": "https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_mini_2026-03.zim",
  "sha256_url": "https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_mini_2026-03.zim.sha256",
  "target": "kiwix/wikipedia_en_all_mini_2026-03.zim",
  "size_bytes": 12405329236
}
```

No `sha256` field — the `.sha256` sidecar at `sha256_url` is the
authoritative checksum (Kiwix sometimes regenerates byte-identical
hashes without renaming).

#### `source: "kolibri_channel"`

```json
{
  "id": "kolibri-channel-khan-math-es",
  "source": "kolibri_channel",
  "studio_base_url": "https://studio.learningequality.org",
  "channel_id": "<32-char hex>",
  "include_node_ids": null,
  "approx_size_bytes": 8000000000
}
```

Not a file download — the First Run app invokes
`kolibri manage importchannel/importcontent` inside the Kolibri
container.

#### `source: "r2"`

```json
{
  "id": "chroma-simplewiki-part-01",
  "source": "r2",
  "url": "https://cdn.<ourdomain>/aibox/chroma/v1/simplewiki_chunks_part_01.tar.zst",
  "target": "backend-data/chroma_db/simplewiki_chunks_part_01.tar.zst",
  "extract_to": "backend-data/chroma_db/",
  "size_bytes": 4294967296,
  "sha256": "<full-file sha256>"
}
```

R2 shards are tar.zst archives extracted in place after verification.

## Signature

Alongside `manifest-<v>.json` we publish `manifest-<v>.json.sig` — a
64-byte ed25519 signature over the canonical JSON-encoded manifest
(sorted keys, no trailing whitespace, UTF-8). The WPF First Run app
verifies it against the public key embedded at
`first-run/Resources/release-pubkey.ed25519`.

The reference Python implementation is in
`../build/build-manifest.py` (sign) and `../build/verify-manifest.py`
(verify). The C# verifier in the WPF app uses `System.Security.Cryptography`'s
`Ed25519` (.NET 8+) for the same bytes.

## Cutting a new manifest

See `../build/README.md` for the `build-manifest.py` workflow.

## Files

- `manifest-0.0.1.json` — sample manifest for end-to-end testing, with
  one item per source type and tiny payloads. NOT a real release.
- `manifest-<release>.json` — real release manifests (none cut yet).
