namespace AIBox.FirstRun.Services;

/// <summary>
/// Compile-time constants the CI release workflow rewrites before
/// signing the binary. Defaults here are for local dev only — the
/// signed manifest URL and target version come from the embedded
/// public key + release.yml at release time.
/// </summary>
public static class BuildConstants
{
    /// <summary>Manifest version this build expects. CI rewrites to the release version.</summary>
    public const string ManifestVersion = "1.0.0";

    /// <summary>Base URL serving manifest-&lt;version&gt;.json and its .sig. CI rewrites for production.</summary>
    public const string ManifestBaseUrl = "https://cdn.projectpuenteai.org/aibox";

    /// <summary>Embedded ed25519 release public key resource path.</summary>
    public const string ReleasePubKeyResource = "Resources/release-pubkey.ed25519";

    /// <summary>Diagnostic label for the ai-control container. The compose file builds locally
    /// (no image: ref), so no registry pin is needed at runtime — this string is shown in logs only.</summary>
    public const string AiControlImageRef = "local-build (compose build context)";

    /// <summary>Optional HF token env var. UI may surface an advanced field that sets this.</summary>
    public const string HfTokenEnvVar = "AIBOX_HF_TOKEN";
}
