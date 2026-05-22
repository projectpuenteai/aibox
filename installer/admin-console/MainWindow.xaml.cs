using System.Windows;
using System.Windows.Controls;
using AIBox.AdminConsole.Services;
using AIBox.AdminConsole.ViewModels;

namespace AIBox.AdminConsole;

public partial class MainWindow : Window
{
    private readonly MainViewModel _vm;
    private bool _suppressTabSync;

    public MainWindow()
    {
        InitializeComponent();

        var prefs = PreferencesStore.Load();
        _vm = new MainViewModel { Language = prefs.Language };
        DataContext = _vm;

        if (prefs.WindowWidth is { } w && w > 0) Width = w;
        if (prefs.WindowHeight is { } h && h > 0) Height = h;

        Loaded   += OnLoaded;
        Closing  += OnClosing;
    }

    private void OnLoaded(object sender, RoutedEventArgs e)
    {
        _suppressTabSync = true;
        LangTabs.SelectedIndex = _vm.Language == Translations.LangEn ? 1 : 0;
        _suppressTabSync = false;

        _vm.StartStatusTimer();
    }

    private void OnClosing(object? sender, System.ComponentModel.CancelEventArgs e)
    {
        if (_vm.IsTransitioning)
        {
            var result = MessageBox.Show(
                this,
                Translations.T(_vm.Language, "confirmCloseBody"),
                Translations.T(_vm.Language, "confirmCloseTitle"),
                MessageBoxButton.YesNo,
                MessageBoxImage.Warning,
                MessageBoxResult.No);
            if (result != MessageBoxResult.Yes)
            {
                e.Cancel = true;
                return;
            }
        }

        _vm.StopStatusTimer();
        PreferencesStore.Save(new Preferences
        {
            Language     = _vm.Language,
            WindowWidth  = Width,
            WindowHeight = Height,
        });
    }

    private void LangTabs_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        // SelectionChanged bubbles from descendant Selectors (e.g. the future
        // possibility of a ComboBox in the content). Guard so we only react to
        // the tab control itself.
        if (e.OriginalSource is not TabControl) return;
        if (_suppressTabSync) return;

        _vm.Language = LangTabs.SelectedIndex == 1 ? Translations.LangEn : Translations.LangEs;
        PreferencesStore.Save(new Preferences
        {
            Language     = _vm.Language,
            WindowWidth  = Width,
            WindowHeight = Height,
        });
    }
}
