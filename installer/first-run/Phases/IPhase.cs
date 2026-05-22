using System;
using System.Threading;
using System.Threading.Tasks;

namespace AIBox.FirstRun.Phases;

public enum PhaseResultKind
{
    AdvanceToNextPhase,
    Done,
    Cancelled,
    Failed,
}

public sealed class PhaseResult
{
    public required PhaseResultKind Kind { get; init; }
    public string Message { get; init; } = "";
    public Exception? Exception { get; init; }

    public static PhaseResult Advance() => new() { Kind = PhaseResultKind.AdvanceToNextPhase };
    public static PhaseResult Done() => new() { Kind = PhaseResultKind.Done };
    public static PhaseResult Cancelled() => new() { Kind = PhaseResultKind.Cancelled };
    public static PhaseResult Failed(string msg, Exception? ex = null)
        => new() { Kind = PhaseResultKind.Failed, Message = msg, Exception = ex };
}

public interface IPhase
{
    event EventHandler<string>? StatusChanged;
    Task<PhaseResult> RunAsync(CancellationToken ct);
}
