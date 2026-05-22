using System;
using System.Linq;
using System.Windows.Controls;

namespace AIBox.FirstRun.Phases;

public partial class AdminPromptView : UserControl
{
    private const int MinLength = 8;

    public AdminPromptView()
    {
        InitializeComponent();
    }

    public bool TryGetInputs(out string username, out string password, out string error)
    {
        username = (UsernameBox.Text ?? "").Trim();
        password = PasswordBox.Password ?? "";
        var confirm = ConfirmBox.Password ?? "";

        if (username.Length < 3 || !username.All(c => char.IsLetterOrDigit(c) || c == '_' || c == '.' || c == '-'))
        {
            error = "Username must be at least 3 characters of letters, digits, and ._-";
            return false;
        }
        if (password.Length < MinLength)
        {
            error = $"Password must be at least {MinLength} characters.";
            return false;
        }
        if (password != confirm)
        {
            error = "Password and confirmation do not match.";
            return false;
        }
        error = "";
        return true;
    }

    private void OnPasswordChanged(object sender, System.Windows.RoutedEventArgs e)
    {
        var pwd = PasswordBox.Password ?? "";
        StrengthLabel.Text = "Strength: " + EstimateStrength(pwd);
    }

    private static string EstimateStrength(string pwd)
    {
        if (pwd.Length == 0) return "—";
        int score = 0;
        if (pwd.Length >= 8) score++;
        if (pwd.Length >= 12) score++;
        if (pwd.Length >= 16) score++;
        if (pwd.Any(char.IsUpper) && pwd.Any(char.IsLower)) score++;
        if (pwd.Any(char.IsDigit)) score++;
        if (pwd.Any(c => !char.IsLetterOrDigit(c))) score++;
        return score switch
        {
            <= 1 => "very weak",
            2 => "weak",
            3 => "fair",
            4 => "good",
            5 => "strong",
            _ => "excellent",
        };
    }
}
