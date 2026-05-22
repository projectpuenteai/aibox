using System.Collections.Specialized;
using System.Windows;
using System.Windows.Controls;
using AIBox.AdminConsole.ViewModels;

namespace AIBox.AdminConsole.Pages;

public partial class ControlPanelView : UserControl
{
    private INotifyCollectionChanged? _subscribedSource;

    public ControlPanelView()
    {
        InitializeComponent();
        Loaded   += OnLoaded;
        Unloaded += OnUnloaded;
    }

    private void OnLoaded(object sender, RoutedEventArgs e)
    {
        if (DataContext is MainViewModel vm)
        {
            _subscribedSource = vm.ConsoleLines;
            _subscribedSource.CollectionChanged += OnConsoleLinesChanged;
            ScrollConsoleToEnd();
        }
    }

    private void OnUnloaded(object sender, RoutedEventArgs e)
    {
        if (_subscribedSource is not null)
        {
            _subscribedSource.CollectionChanged -= OnConsoleLinesChanged;
            _subscribedSource = null;
        }
    }

    private void OnConsoleLinesChanged(object? sender, NotifyCollectionChangedEventArgs e)
    {
        if (e.Action == NotifyCollectionChangedAction.Add ||
            e.Action == NotifyCollectionChangedAction.Reset)
        {
            // Dispatcher.BeginInvoke at Background priority lets WPF realize
            // the new item before we try to ScrollIntoView it.
            Dispatcher.BeginInvoke(new System.Action(ScrollConsoleToEnd),
                System.Windows.Threading.DispatcherPriority.Background);
        }
    }

    private void ScrollConsoleToEnd()
    {
        if (ConsoleList.Items.Count == 0) return;
        var last = ConsoleList.Items[ConsoleList.Items.Count - 1];
        ConsoleList.ScrollIntoView(last);
    }
}
