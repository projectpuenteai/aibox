using System;
using System.Threading.Tasks;
using System.Windows.Input;

namespace AIBox.AdminConsole.ViewModels;

/// <summary>
/// Minimal ICommand for async handlers. Re-entrancy guarded via an internal
/// busy flag so a button can be clicked at most once per logical action.
/// </summary>
public sealed class RelayCommand : ICommand
{
    private readonly Func<Task> _execute;
    private readonly Func<bool>? _canExecute;
    private bool _busy;

    public RelayCommand(Func<Task> execute, Func<bool>? canExecute = null)
    {
        _execute = execute;
        _canExecute = canExecute;
    }

    public event EventHandler? CanExecuteChanged;

    public bool CanExecute(object? parameter) =>
        !_busy && (_canExecute?.Invoke() ?? true);

    public async void Execute(object? parameter)
    {
        if (_busy) return;
        _busy = true;
        RaiseCanExecuteChanged();
        try
        {
            await _execute().ConfigureAwait(true);
        }
        finally
        {
            _busy = false;
            RaiseCanExecuteChanged();
        }
    }

    public void RaiseCanExecuteChanged() =>
        CanExecuteChanged?.Invoke(this, EventArgs.Empty);
}
