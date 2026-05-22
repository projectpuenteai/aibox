using System;
using System.Collections.Generic;
using System.Reflection;
using Xunit;

namespace AIBox.FirstRun.Tests;

/// <summary>
/// Tests for the internal <c>GlobMatcher</c> class in HuggingFaceFetcher.cs.
///
/// GlobMatcher is <c>internal sealed</c>. Rather than requiring an
/// [assembly: InternalsVisibleTo] attribute on the production assembly (which
/// would touch a production source file), we access the type via reflection.
/// This is acceptable for unit tests whose sole purpose is white-box verification
/// of private/internal helpers.
///
/// If the project later adds [assembly: InternalsVisibleTo("AIBoxFirstRunTests")]
/// to first-run/Properties/AssemblyInfo.cs, these tests can be rewritten to
/// use the type directly without reflection.
/// </summary>
public sealed class GlobMatcherTests
{
    // -------------------------------------------------------------------------
    // Reflection-based thin wrapper around the internal GlobMatcher
    // -------------------------------------------------------------------------

    private sealed class GlobMatcher
    {
        private static readonly Type? _type = typeof(AIBox.FirstRun.Services.ManifestParser)
            .Assembly
            .GetType("AIBox.FirstRun.Services.Fetchers.GlobMatcher", throwOnError: false);

        private static readonly ConstructorInfo? _ctor =
            _type?.GetConstructor(new[] { typeof(IEnumerable<string>) });

        private static readonly MethodInfo? _match =
            _type?.GetMethod("Match", new[] { typeof(string) });

        private readonly object? _instance;

        public GlobMatcher(IEnumerable<string> globs)
        {
            if (_ctor == null)
                throw new InvalidOperationException(
                    "Could not find GlobMatcher(IEnumerable<string>) constructor via reflection. " +
                    "Check that the type name 'AIBox.FirstRun.Services.Fetchers.GlobMatcher' is still correct.");
            _instance = _ctor.Invoke(new object[] { globs });
        }

        public bool Match(string path)
        {
            if (_match == null || _instance == null)
                throw new InvalidOperationException("GlobMatcher.Match not found via reflection.");
            return (bool)_match.Invoke(_instance, new object[] { path })!;
        }
    }

    private static GlobMatcher Make(params string[] globs) => new GlobMatcher(globs);

    // -------------------------------------------------------------------------
    // Sanity: reflection wiring works
    // -------------------------------------------------------------------------

    [Fact]
    public void ReflectionWiring_CanConstructAndCallGlobMatcher()
    {
        // If GlobMatcher moved or was renamed, this test will fail with a clear message.
        var m = Make("*.json");
        Assert.True(m.Match("config.json"));
    }

    // -------------------------------------------------------------------------
    // *.json — single-segment wildcard
    // -------------------------------------------------------------------------

    [Fact]
    public void SingleStar_MatchesFileInRoot()
    {
        Assert.True(Make("*.json").Match("config.json"));
    }

    [Fact]
    public void SingleStar_DoesNotMatchFileInSubdir()
    {
        // '*' must not cross a path separator.
        Assert.False(Make("*.json").Match("sub/config.json"));
    }

    // -------------------------------------------------------------------------
    // **/*.json — cross-segment wildcard
    // -------------------------------------------------------------------------

    [Fact]
    public void DoubleStar_MatchesFileInRoot()
    {
        Assert.True(Make("**/*.json").Match("config.json"));
    }

    [Fact]
    public void DoubleStar_MatchesFileInSubdir()
    {
        Assert.True(Make("**/*.json").Match("sub/config.json"));
    }

    [Fact]
    public void DoubleStar_MatchesFileInDeeplyNestedSubdir()
    {
        Assert.True(Make("**/*.json").Match("a/b/c/config.json"));
    }

    // -------------------------------------------------------------------------
    // onnx/** — prefix with double-star suffix
    // -------------------------------------------------------------------------

    [Fact]
    public void PrefixDoubleStar_MatchesDirectChild()
    {
        Assert.True(Make("onnx/**").Match("onnx/model.onnx"));
    }

    [Fact]
    public void PrefixDoubleStar_MatchesNestedChild()
    {
        Assert.True(Make("onnx/**").Match("onnx/sub/file.txt"));
    }

    [Fact]
    public void PrefixDoubleStar_DoesNotMatchSiblingDir()
    {
        Assert.False(Make("onnx/**").Match("weights/model.onnx"));
    }

    // -------------------------------------------------------------------------
    // Empty pattern list — matches everything
    // -------------------------------------------------------------------------

    [Fact]
    public void EmptyPatternList_MatchesAnyPath()
    {
        var m = Make(); // no patterns
        Assert.True(m.Match("anything.bin"));
        Assert.True(m.Match("nested/path/file.txt"));
        Assert.True(m.Match(""));
    }

    // -------------------------------------------------------------------------
    // Multiple patterns — OR semantics
    // -------------------------------------------------------------------------

    [Fact]
    public void MultiplePatterns_MatchesIfAnyPatternMatches()
    {
        var m = Make("*.json", "*.txt");
        Assert.True(m.Match("config.json"));
        Assert.True(m.Match("README.txt"));
        Assert.False(m.Match("model.bin"));
    }

    // -------------------------------------------------------------------------
    // Literal pattern — exact file name
    // -------------------------------------------------------------------------

    [Fact]
    public void LiteralPattern_MatchesExactName()
    {
        Assert.True(Make("LICENSE").Match("LICENSE"));
        Assert.False(Make("LICENSE").Match("LICENSE.txt"));
        Assert.False(Make("LICENSE").Match("sub/LICENSE"));
    }

    // -------------------------------------------------------------------------
    // Question mark — single non-separator char
    // -------------------------------------------------------------------------

    [Fact]
    public void QuestionMark_MatchesSingleChar()
    {
        Assert.True(Make("file?.bin").Match("fileA.bin"));
        Assert.False(Make("file?.bin").Match("file.bin"));   // zero chars — no match
        Assert.False(Make("file?.bin").Match("fileAB.bin")); // two chars — no match
    }
}
