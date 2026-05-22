using System.Windows;
using System.Windows.Controls;

namespace AIBox.FirstRun.Phases;

public partial class SummaryView : UserControl
{
    private string _password = "";
    private bool _shown;

    public SummaryView()
    {
        InitializeComponent();
    }

    public void SetCredentials(string username, string password)
    {
        UsernameBox.Text = username;
        _password = password;
        PasswordBox.Text = Mask(password);
    }

    private void OnShowClick(object sender, RoutedEventArgs e)
    {
        _shown = !_shown;
        PasswordBox.Text = _shown ? _password : Mask(_password);
    }

    private void OnCopyClick(object sender, RoutedEventArgs e)
    {
        try
        {
            Clipboard.SetText(_password);
        }
        catch
        {
            MessageBox.Show("Could not copy to clipboard. The password is displayed above when you click Show.",
                "AIBox First Run", MessageBoxButton.OK, MessageBoxImage.Information);
        }
    }

    private static string Mask(string s) => new string('•', s.Length);
}
